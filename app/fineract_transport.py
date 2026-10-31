"""Pinned, self-hosted Apache Fineract demo transport. Never a live bank route.

All network destinations are literal loopback HTTP. Production credentials,
remote hosts, arbitrary redirects and automatic write retries are prohibited.
"""
from __future__ import annotations
import base64,copy,hashlib,http.client,json,sqlite3,threading,time
from contextvars import ContextVar
from contextlib import contextmanager
from datetime import datetime,timezone,timedelta
from decimal import Decimal
from urllib.parse import urlsplit


class FineractUnavailable(ConnectionError):pass


def cents(value):
    if type(value) not in {int,Decimal,str}:raise ValueError('money must be an exact decimal')
    try:
        number=Decimal(value)
        if not number.is_finite() or number<0 or number*100!=(number*100).to_integral_value():raise ValueError('invalid cents precision')
        return int(number*100)
    except Exception as exc:raise ValueError('invalid exact money') from exc


def decimal_amount(value):
    if type(value) is not int or value<=0:raise ValueError('positive integer cents required')
    return format(Decimal(value)/100,'.2f')


class FineractAPI:
    def __init__(self,base_url,username,password,tenant='default'):
        target=urlsplit(base_url)
        if (target.scheme!='http' or target.hostname not in {'127.0.0.1','::1'} or target.username or target.password
            or target.query or target.fragment or target.path.rstrip('/')!='/fineract-provider'
            or not isinstance(username,str) or not username or not isinstance(password,str) or not password or
            any(c in username+password+tenant for c in '\r\n') or not isinstance(tenant,str) or not tenant.isalnum()):raise ValueError('local demo endpoint and credentials required')
        self._host=target.hostname;self._port=target.port or 80;self._auth=base64.b64encode((username+':'+password).encode()).decode();self._tenant=tenant;self.last_error=None
        self._deadline=ContextVar('fineract_http_deadline',default=None)
    @contextmanager
    def deadline(self,deadline):
        token=self._deadline.set(deadline)
        try:yield
        finally:self._deadline.reset(token)
    def __call__(self,method,path,body=None,key=''):
        if method not in {'GET','POST','PUT','DELETE'} or not isinstance(path,str) or not path.startswith('/') or path.startswith('//') or '\r' in path or '\n' in path:raise ValueError('invalid API request')
        deadline=self._deadline.get();remaining=20 if deadline is None else deadline-time.monotonic()
        if remaining<=0:raise FineractUnavailable('core request deadline exceeded')
        connection=http.client.HTTPConnection(self._host,self._port,timeout=min(20,remaining))
        try:
            headers={'Authorization':'Basic '+self._auth,'Fineract-Platform-TenantId':self._tenant,'Content-Type':'application/json'}
            if key:headers['Idempotency-Key']=key
            raw=None if body is None else json.dumps(body,allow_nan=False).encode()
            connection.request(method,'/fineract-provider/api/v1'+path,raw,headers)
            response=connection.getresponse();raw=response.read(8*1024*1024+1)
            if len(raw)>8*1024*1024:raise FineractUnavailable('local Fineract response too large')
            if not 200<=response.status<300:
                try:
                    data=json.loads(raw)
                    self.last_error={'status':response.status,'errors':[{k:item.get(k) for k in ('parameterName','userMessageGlobalisationCode')} for item in data.get('errors',[])]}
                except (ValueError,TypeError,AttributeError):self.last_error={'status':response.status}
                raise FineractUnavailable('local Fineract HTTP status '+str(response.status))
            return json.loads(raw,parse_float=Decimal) if raw else {}
        except (OSError,ValueError,http.client.HTTPException) as exc:raise FineractUnavailable('local Fineract response unavailable') from exc
        finally:connection.close()


class FineractTransport:
    scope='FINERACT_LOCAL_SIMULATOR'
    def __init__(self,path,requester,configuration):
        self.api=requester;self.config=copy.deepcopy(configuration);self.lock=threading.RLock()
        if any(type(self.config.get(k)) is not int or self.config[k]<=0 for k in ('account_id','client_id')):raise ValueError('trusted core account/client binding required')
        if not isinstance(self.config.get('beneficiaries'),dict):raise ValueError('trusted beneficiary map required')
        self.db=sqlite3.connect(str(path),check_same_thread=False);self.db.row_factory=sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS fineract_requests(request_id TEXT PRIMARY KEY,fingerprint TEXT NOT NULL,request_json TEXT NOT NULL,provider_id INTEGER,receipt_json TEXT)');self.db.commit();self.closed=False
    def close(self):
        with self.lock:
            if not self.closed:self.db.close();self.closed=True
    @staticmethod
    def _dump(value):return json.dumps(value,sort_keys=True,separators=(',',':'),allow_nan=False)
    def _list_transfers(self):
        result=[];offset=0
        while True:
            page=self.api('GET',f'/accounttransfers?offset={offset}&limit=100')
            if not isinstance(page,dict) or not isinstance(page.get('pageItems'),list) or type(page.get('totalFilteredRecords')) is not int:raise FineractUnavailable('unverified transfer list')
            total=page['totalFilteredRecords'];items=page['pageItems']
            if total>1000 or total<0 or len(items)>100 or offset>1000:raise FineractUnavailable('transfer scan exceeds bounded demo scope')
            result.extend(items)
            if len(result)>=total:return result
            if not items:raise FineractUnavailable('incomplete transfer list')
            offset+=len(items)
    def _reply(self,request,state,payload,provider_id,sequence=1):
        result={k:request[k] for k in ('schema','operation','request_id','actor_id','subject_id','account_ref','idempotency_key')}
        result.update(status=state,payload=payload,provider_request_id='fineract-'+str(provider_id),sequence=sequence);return result
    def _balance(self,request):
        account=self.api('GET',f"/savingsaccounts/{self.config['account_id']}")
        try:
            if account['id']!=self.config['account_id'] or account['clientId']!=self.config['client_id'] or account['currency']['code']!='CNY' or not account['status']['active']:raise ValueError('account mismatch')
            available=cents(account['summary']['availableBalance']);today=list(datetime.now(timezone(timedelta(hours=8))).date().timetuple()[:3]);daily=0
            for row in self._list_transfers():
                if row['fromAccount']['id']==self.config['account_id'] and row['transferDate']==today and row['reversed'] is False:daily+=cents(row['transferAmount'])
            payload={'available_cents':available,'daily_transfer_cents':daily,'currency':'CNY','as_of':datetime.now(timezone.utc).isoformat()}
            return self._reply(request,'succeeded',payload,'account-'+str(account['id']))
        except (KeyError,TypeError,ValueError):raise FineractUnavailable('core balance identity/units not verified')
    def _normalize_transfer(self,request,provider_id):
        row=self.api('GET','/accounttransfers/'+str(provider_id));expected=request['payload'];original=request
        if request['operation']=='reverse_transfer':
            saved=self.db.execute("SELECT request_json FROM fineract_requests WHERE provider_id=? AND json_extract(request_json,'$.operation')='create_transfer'",(provider_id,)).fetchone()
            if not saved:raise FineractUnavailable('original core transfer not bound')
            original=json.loads(saved[0])
            if any(request[k]!=original[k] for k in ('actor_id','subject_id','account_ref')):raise FineractUnavailable('reversal subject mismatch')
        try:
            target=self.config['beneficiaries'][expected['beneficiary_id']]
            if (row['id']!=provider_id or row['fromAccount']['id']!=self.config['account_id'] or row['toAccount']['id']!=target['account_id']
                or row['currency']['code']!='CNY' or cents(row['transferAmount'])!=expected['amount_cents'] or row['transferDescription']!=original['request_id']
                or type(row['reversed']) is not bool):raise ValueError('core receipt mismatch')
            state='reversed' if row['reversed'] else 'succeeded'
            if request['operation']=='reverse_transfer' and state!='reversed':raise FineractUnavailable('core reversal not verified')
            payload={'amount_cents':expected['amount_cents'],'beneficiary_id':expected['beneficiary_id'],'currency':'CNY','transaction_id':'fineract-transfer-'+str(provider_id)}
            prior=self.db.execute('SELECT receipt_json FROM fineract_requests WHERE request_id=?',(request['request_id'],)).fetchone()
            old=json.loads(prior[0]) if prior and prior[0] else None;sequence=old['sequence']+(old['status']!=state) if old else 1
            result=self._reply(request,state,payload,('undo-' if request['operation']=='reverse_transfer' else 'transfer-')+str(provider_id),sequence)
            self.db.execute('UPDATE fineract_requests SET provider_id=?,receipt_json=? WHERE request_id=?',(provider_id,self._dump(result),request['request_id']));self.db.commit();return result
        except (KeyError,TypeError,ValueError):raise FineractUnavailable('core transfer evidence not verified')
    def _query(self,request):
        saved=self.db.execute('SELECT * FROM fineract_requests WHERE request_id=?',(request['request_id'],)).fetchone()
        if not saved:raise FineractUnavailable('original core request not journaled')
        provider_id=saved['provider_id']
        if provider_id is None:
            matching=[r for r in self._list_transfers() if r.get('transferDescription')==request['request_id'] and r.get('fromAccount',{}).get('id')==self.config['account_id']]
            if len(matching)!=1:raise FineractUnavailable('original acceptance unresolved; no resubmission')
            provider_id=matching[0]['id']
        return self._normalize_transfer(request,provider_id)
    def _transfer(self,request):
        payload=request['payload'];target=self.config['beneficiaries'].get(payload.get('beneficiary_id'))
        if not target or payload.get('currency')!='CNY':raise FineractUnavailable('unmapped target')
        amount=decimal_amount(payload['amount_cents']);encoded=self._dump(request);fingerprint=hashlib.sha256(encoded.encode()).hexdigest()
        saved=self.db.execute('SELECT * FROM fineract_requests WHERE request_id=?',(request['request_id'],)).fetchone()
        if saved:
            if saved['fingerprint']!=fingerprint:raise FineractUnavailable('transport request changed')
            return self._query(request)
        # Persist before POST. A crash/timeout, even before send, is not a retry grant.
        self.db.execute('INSERT INTO fineract_requests VALUES(?,?,?,?,?)',(request['request_id'],fingerprint,encoded,None,None));self.db.commit()
        body={'fromOfficeId':1,'fromClientId':self.config['client_id'],'fromAccountType':2,'fromAccountId':self.config['account_id'],
            'toOfficeId':1,'toClientId':target['client_id'],'toAccountType':2,'toAccountId':target['account_id'],
            'transferDate':datetime.now(timezone(timedelta(hours=8))).date().isoformat(),'transferAmount':amount,
            'transferDescription':request['request_id'],'dateFormat':'yyyy-MM-dd','locale':'en'}
        if self.config.get('payment_type_id'):body['paymentTypeId']=self.config['payment_type_id']
        accepted=self.api('POST','/accounttransfers',body,request['idempotency_key'])
        provider_id=accepted.get('resourceId')
        if type(provider_id) is not int or provider_id<=0:raise FineractUnavailable('core acceptance id missing')
        self.db.execute('UPDATE fineract_requests SET provider_id=? WHERE request_id=?',(provider_id,request['request_id']));self.db.commit()
        return self._normalize_transfer(request,provider_id)
    def _statement(self,request):
        items=[];known={r['request_id']:json.loads(r['request_json']) for r in self.db.execute('SELECT * FROM fineract_requests')}
        for record in self._list_transfers():
            if record.get('fromAccount',{}).get('id')!=self.config['account_id']:continue
            rid=record.get('transferDescription')
            if rid in known:items.append(self._normalize_transfer(known[rid],record['id']))
            else:items.append({'request_id':'core-unmapped-'+str(record.get('id')),'core_record_id':record.get('id')})
        items.extend(self._query(r) for r in known.values() if r['operation']=='reverse_transfer')
        return {'schema':1,'subject_id':request['subject_id'],'account_ref':request['account_ref'],'items':items}
    def _reverse(self,request):
        payload=request['payload'];reference=payload.get('transaction_id','')
        if not isinstance(reference,str) or not reference.startswith('fineract-transfer-') or not reference[18:].isdigit():raise FineractUnavailable('invalid original core transfer reference')
        provider_id=int(reference[18:]);saved=self.db.execute("SELECT request_json FROM fineract_requests WHERE provider_id=? AND json_extract(request_json,'$.operation')='create_transfer'",(provider_id,)).fetchone()
        if not saved:raise FineractUnavailable('unowned core transfer')
        original=json.loads(saved[0])
        if any(request[k]!=original[k] for k in ('actor_id','subject_id','account_ref')) or any(payload[k]!=original['payload'][k] for k in ('amount_cents','currency','beneficiary_id')):raise FineractUnavailable('original reversal binding mismatch')
        encoded=self._dump(request);fingerprint=hashlib.sha256(encoded.encode()).hexdigest();prior=self.db.execute('SELECT * FROM fineract_requests WHERE request_id=?',(request['request_id'],)).fetchone()
        if prior:
            if prior['fingerprint']!=fingerprint:raise FineractUnavailable('reversal request changed')
            return self._query(request)
        self.db.execute('INSERT INTO fineract_requests VALUES(?,?,?,?,?)',(request['request_id'],fingerprint,encoded,provider_id,None));self.db.commit()
        self.api('POST',f'/accounttransfers/{provider_id}?command=undo',{},request['idempotency_key'])
        return self._normalize_transfer(request,provider_id)
    def send(self,operation,request,timeout):
        if operation not in {'get_balance','create_transfer','query_request','statement','reverse_transfer'}:raise FineractUnavailable('core operation unsupported; no simulated success')
        if type(timeout) not in {int,float} or not 0<timeout<=10:raise ValueError('bounded timeout required')
        with self.lock:
            if isinstance(self.api,FineractAPI):
                with self.api.deadline(time.monotonic()+timeout):return self._send(operation,request)
            return self._send(operation,request)
    def _send(self,operation,request):
            try:
                if operation=='get_balance':return self._balance(request)
                if operation=='create_transfer':return self._transfer(request)
                if operation=='query_request':return self._query(request)
                if operation=='reverse_transfer':return self._reverse(request)
                return self._statement(request)
            except FineractUnavailable:raise
            except (KeyError,TypeError,ValueError,sqlite3.Error,OSError) as exc:raise FineractUnavailable('local core evidence unavailable') from exc
