"""P07 project-owned bank protocol boundary; stdlib only, no live bank transport.

No URL/credential is accepted from chat. An institution-specific authenticated
transport and production identity/authorization must be implemented separately.
The integrated facade accepts approved local protocol or pinned Fineract demo only.
"""
from __future__ import annotations
import copy,hashlib,hmac,json,re,sqlite3,threading,time,uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import asdict,dataclass
from datetime import datetime
from production_contracts import AdapterContext,AdapterResult

STATES={'accepted':'processing','created':'processing','processing':'processing','unknown':'unknown',
        'succeeded':'succeeded','failed':'failed','reversed':'reversed'}
WRITES={'create_transfer','report_loss','unreport_loss','reverse_transfer'}
_ACTIVE=ContextVar('bank_service_context',default=None)


class BankServiceError(ConnectionError):pass


@dataclass(frozen=True)
class BankBinding:
    actor_id:str
    subject_id:str
    account_ref:str
    authorization_ref:str
    authorization_version:int
    beneficiaries:dict
    card_ids:tuple
    device_ids:tuple=('demo-device',)


def _id(value):return isinstance(value,str) and bool(re.fullmatch(r'[A-Za-z0-9_:.\-]{1,192}',value))
def _json(value):return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)
def _sha(value):return hashlib.sha256(_json(value).encode()).hexdigest()


class BankServiceAdapter:
    """Durable at-most-once *local dispatch*, not exactly-once bank certification.

    transport.send(operation, request_dict, timeout_seconds) must implement this
    project's normalized profile or translate a provider's documented protocol.
    It must bound I/O, authenticate the peer and NEVER retry a write itself.
    """
    def __init__(self,path,transport,binding_resolver,audit,*,callback_secret=None,supports_reversal=False):
        if not callable(binding_resolver) or not callable(audit):raise ValueError('trusted resolver and audit sink required')
        self.transport=transport;self.resolve_binding=binding_resolver;self.audit=audit
        self.service_scope=getattr(transport,'scope','LOCAL_PROTOCOL_TEST')
        if self.service_scope not in {'LOCAL_PROTOCOL_TEST','FINERACT_LOCAL_SIMULATOR'}:raise ValueError('unapproved bank service scope')
        self.callback_secret=callback_secret;self.supports_reversal=supports_reversal
        self.lock=threading.RLock();self.db=sqlite3.connect(str(path),timeout=3,check_same_thread=False);self.db.row_factory=sqlite3.Row
        self.db.executescript('''CREATE TABLE IF NOT EXISTS bank_requests (
          request_id TEXT PRIMARY KEY, idem TEXT UNIQUE NOT NULL, fingerprint TEXT NOT NULL,
          context TEXT NOT NULL, operation TEXT NOT NULL, payload TEXT NOT NULL,
          status TEXT NOT NULL, sequence INTEGER NOT NULL DEFAULT 0, receipt TEXT, updated_at REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS bank_callback_events(event_id TEXT PRIMARY KEY, body_sha TEXT NOT NULL, request_id TEXT NOT NULL);
        ''');self.db.commit();self.closed=False
    def close(self):
        with self.lock:
            if not self.closed:
                self.db.close();self.closed=True
                closer=getattr(self.transport,'close',None)
                if callable(closer):closer()
    def context(self,actor_id,*,device_id='demo-device',trace_id='bank-protocol-read',idempotency_key='',operation_digest=''):
        b=self.resolve_binding(actor_id)
        if not isinstance(b,BankBinding):raise BankServiceError('bank account binding unavailable')
        return AdapterContext(trace_id,actor_id,device_id,idempotency_key,b.subject_id,b.account_ref,b.authorization_ref,b.authorization_version,operation_digest)
    @contextmanager
    def scope(self,ctx):
        token=_ACTIVE.set(ctx)
        try:yield
        finally:_ACTIVE.reset(token)
    def _binding(self,ctx,write=False):
        b=self.resolve_binding(ctx.actor_id)
        if (not isinstance(b,BankBinding) or b.actor_id!=ctx.actor_id or b.subject_id!=ctx.subject_id
            or b.actor_id!=b.subject_id or b.account_ref!=ctx.account_ref or b.authorization_ref!=ctx.authorization_ref
            or type(ctx.authorization_version) is not int or ctx.authorization_version!=b.authorization_version
            or ctx.device_id not in b.device_ids or not all(_id(v) for v in (ctx.trace_id,ctx.actor_id,ctx.subject_id,ctx.account_ref,ctx.authorization_ref))):
            raise ValueError('trusted account authorization mismatch')
        if write and (not _id(ctx.idempotency_key) or not re.fullmatch('[0-9a-f]{64}',ctx.operation_digest)):raise ValueError('bound operation and idempotency key required')
        return b
    def _result(self,ctx,code,data=None,ok=True):
        return AdapterResult(ok,code,'银行协议结果已返回。' if ok else '银行服务边界拒绝或暂不可用，请核对原请求。',data or {},ctx.trace_id,
                             (data or {}).get('request_id','bank-'+uuid.uuid4().hex),ctx.idempotency_key,False)
    def _event(self,ctx,event,**data):self.audit(ctx,event,data)
    def _request(self,ctx,operation,payload,request_id=None):
        return {'schema':1,'operation':operation,'request_id':request_id or 'bankreq_'+uuid.uuid4().hex.translate(str.maketrans('0123456789','ghijklmnop')),
            'actor_id':ctx.actor_id,'subject_id':ctx.subject_id,'account_ref':ctx.account_ref,
            'authorization_ref':ctx.authorization_ref,'authorization_version':ctx.authorization_version,
            'device_id':ctx.device_id,'trace_id':ctx.trace_id,'operation_digest':ctx.operation_digest,
            'idempotency_key':ctx.idempotency_key,'payload':copy.deepcopy(payload)}
    def _reserve(self,request,fingerprint):
        self.db.execute('INSERT INTO bank_requests(request_id,idem,fingerprint,context,operation,payload,status,updated_at) VALUES(?,?,?,?,?,?,?,?)',
            (request['request_id'],request['idempotency_key'],fingerprint,_json({k:request[k] for k in asdict(AdapterContext('',''))}),
             request['operation'],_json(request['payload']),'unknown',time.time()));self.db.commit()
    def _row(self,request_id):return self.db.execute('SELECT * FROM bank_requests WHERE request_id=?',(request_id,)).fetchone()
    def _data(self,row,*,replay=False,uncertain=False):
        p=json.loads(row['payload']);receipt=json.loads(row['receipt']) if row['receipt'] else None
        data={'request_id':row['request_id'],'transaction_id':row['request_id'],'status':'unknown' if uncertain else row['status'],
              'service_scope':self.service_scope,'idempotency_key':row['idem'],'operation':row['operation']}
        if receipt:data.update(copy.deepcopy(receipt['payload']),provider_request_id=receipt['provider_request_id'])
        # Local stable reference survives a lost provider response; never fabricate a provider ID.
        if 'transaction_id' in data and receipt and receipt['payload'].get('transaction_id'):
            data['provider_transaction_id']=receipt['payload']['transaction_id']
        data['transaction_id']=row['request_id']
        for key in ('amount_cents','currency','beneficiary_id','card_id'):
            if key in p:data[key]=p[key]
        if replay:data['idempotent_replay']=True
        if uncertain or row['status']=='unknown':data['outcome_unknown']=True
        return data
    def _validate(self,request,response):
        keys={'schema','operation','request_id','actor_id','subject_id','account_ref','idempotency_key','provider_request_id','status','sequence','payload'}
        if not isinstance(response,dict) or set(response)!=keys or type(response['schema']) is not int or response['schema']!=1:raise ValueError('invalid receipt schema')
        if len(_json(response).encode())>32768:raise ValueError('receipt too large')
        for key in ('operation','request_id','actor_id','subject_id','account_ref','idempotency_key'):
            if response[key]!=request[key]:raise ValueError('receipt binding mismatch')
        if not _id(response['provider_request_id']) or type(response['sequence']) is not int or response['sequence']<1 or response['status'] not in STATES:raise ValueError('invalid receipt state')
        p=response['payload'];expected=request['payload'];operation=request['operation']
        if not isinstance(p,dict):raise ValueError('invalid receipt payload')
        if operation=='get_balance':
            if set(p)!={'available_cents','daily_transfer_cents','currency','as_of'} or any(type(p[k]) is not int or p[k]<0 for k in ('available_cents','daily_transfer_cents')) or p['currency']!='CNY':raise ValueError('invalid balance units')
            stamp=datetime.fromisoformat(p['as_of'])
            if stamp.tzinfo is None or not -5<=time.time()-stamp.timestamp()<=60:raise ValueError('balance timestamp stale or not timezone-aware')
        elif operation in {'create_transfer','reverse_transfer'}:
            if set(p)!={'amount_cents','currency','beneficiary_id','transaction_id'}:raise ValueError('invalid transfer evidence')
            if type(p['amount_cents']) is not int or p['amount_cents']!=expected['amount_cents'] or p['currency']!='CNY' or p['beneficiary_id']!=expected['beneficiary_id'] or not _id(p['transaction_id']):raise ValueError('transfer evidence mismatch')
        else:
            if set(p)!={'card_id','card_status'} or p['card_id']!=expected['card_id'] or p['card_status'] not in {'normal','reported_loss'}:raise ValueError('card evidence mismatch')
            target={'report_loss':'reported_loss','unreport_loss':'normal'}.get(operation)
            if response['status']=='succeeded' and target and p['card_status']!=target:raise ValueError('card final state mismatch')
        return STATES[response['status']]
    def _apply(self,row,response):
        ctx=AdapterContext(**json.loads(row['context']));request=self._request(ctx,row['operation'],json.loads(row['payload']),row['request_id'])
        state=self._validate(request,response)
        if response['sequence']<row['sequence']:raise ValueError('stale receipt')
        if response['sequence']==row['sequence']:
            if _json(response)!=row['receipt']:raise ValueError('conflicting same sequence')
            return
        if row['status'] in {'failed','reversed'} and state!=row['status'] or row['status']=='succeeded' and state not in {'succeeded','reversed'}:raise ValueError('terminal state regression')
        previous=json.loads(row['receipt']) if row['receipt'] else None
        if previous and (previous['provider_request_id']!=response['provider_request_id'] or previous['payload'].get('transaction_id')!=response['payload'].get('transaction_id')):raise ValueError('provider identity changed')
        self._event(ctx,'bank.receipt.verified',request_id=row['request_id'],operation=row['operation'],status=state,sequence=response['sequence'])
        self.db.execute('UPDATE bank_requests SET status=?,sequence=?,receipt=?,updated_at=? WHERE request_id=?',(state,response['sequence'],_json(response),time.time(),row['request_id']));self.db.commit()
    def _submit(self,ctx,operation,payload):
        with self.lock:
            try:
                self._binding(ctx,True)
                fingerprint=_sha({'operation':operation,'actor_id':ctx.actor_id,'subject_id':ctx.subject_id,'account_ref':ctx.account_ref,
                    'authorization_ref':ctx.authorization_ref,'authorization_version':ctx.authorization_version,'device_id':ctx.device_id,
                    'operation_digest':ctx.operation_digest,'payload':payload})
                old=self.db.execute('SELECT * FROM bank_requests WHERE idem=?',(ctx.idempotency_key,)).fetchone()
                if old:
                    if old['fingerprint']!=fingerprint:return self._result(ctx,'IDEMPOTENCY_CONFLICT',ok=False)
                    return self._result(ctx,'BANK_REPLAY',self._data(old,replay=True),old['status']!='failed')
                request=self._request(ctx,operation,payload)
                self._event(ctx,'bank.dispatch.reserved',request_id=request['request_id'],operation=operation)
                self._reserve(request,fingerprint)
            except (ValueError,TypeError,OSError,sqlite3.Error):return self._result(ctx,'BANK_PRE_DISPATCH_REJECTED',ok=False)
            # Reservation is durable before I/O. Any exception thereafter is uncertain;
            # retries/restarts return the same local request and never send again.
            try:
                response=self.transport.send(operation,copy.deepcopy(request),5)
                self._apply(self._row(request['request_id']),response)
            except Exception:
                # I/O may have committed at the bank even when the local journal
                # becomes unreadable. Return the pre-I/O stable reference anyway.
                try:data=self._data(self._row(request['request_id']),uncertain=True)
                except sqlite3.Error:
                    data={'request_id':request['request_id'],'transaction_id':request['request_id'],'status':'unknown',
                          'outcome_unknown':True,'operation':operation,'idempotency_key':ctx.idempotency_key,
                          'service_scope':self.service_scope,**{k:v for k,v in payload.items() if k in {'amount_cents','currency','beneficiary_id','card_id'}}}
                return self._result(ctx,'OUTCOME_UNKNOWN',data)
            row=self._row(request['request_id']);return self._result(ctx,'BANK_RECEIPT',self._data(row),row['status']!='failed')
    def _read(self,ctx,operation,payload):
        with self.lock:
            try:
                self._binding(ctx);request=self._request(ctx,operation,payload)
                self._event(ctx,'bank.read.requested',operation=operation,request_id=request['request_id'])
                response=self.transport.send(operation,copy.deepcopy(request),5);status=self._validate(request,response)
                if status!='succeeded':raise ValueError('read has no usable final evidence')
                return self._result(ctx,'BANK_READ',dict(response['payload'],request_id=request['request_id'],service_scope=self.service_scope))
            except Exception:return self._result(ctx,'BANK_READ_UNAVAILABLE',ok=False)
    def get_balance(self,ctx):return self._read(ctx,'get_balance',{})
    def create_transfer(self,ctx,amount_cents,beneficiary_id,note):
        try:
            b=self._binding(ctx,True)
            if type(amount_cents) is not int or not 0<amount_cents<=100000000 or beneficiary_id not in b.beneficiaries.values() or not isinstance(note,str) or len(note)>120:raise ValueError('invalid transfer')
        except (TypeError,ValueError):return self._result(ctx,'INVALID_TRANSFER_BINDING',ok=False)
        return self._submit(ctx,'create_transfer',{'amount_cents':amount_cents,'beneficiary_id':beneficiary_id,'currency':'CNY','note':note})
    def _card(self,ctx,card_id,operation):
        try:
            if card_id not in self._binding(ctx,operation in WRITES).card_ids:raise ValueError('card not authorized')
        except (TypeError,ValueError):return self._result(ctx,'CARD_ACCESS_DENIED',ok=False)
        return (self._submit if operation in WRITES else self._read)(ctx,operation,{'card_id':card_id})
    def report_loss(self,ctx,card_id):return self._card(ctx,card_id,'report_loss')
    def unreport_loss(self,ctx,card_id):return self._card(ctx,card_id,'unreport_loss')
    def query_card(self,ctx,card_id):return self._card(ctx,card_id,'query_card')
    def record_for_key(self,key):
        with self.lock:
            row=self.db.execute('SELECT * FROM bank_requests WHERE idem=?',(key,)).fetchone()
            return dict(row,transaction_id=row['request_id']) if row else None
    def query_transaction(self,ctx,transaction_id):
        with self.lock:
            try:
                self._binding(ctx);row=self._row(transaction_id)
                if row is None:raise ValueError('unknown local request')
                original=AdapterContext(**json.loads(row['context']))
                if (ctx.actor_id,ctx.subject_id,ctx.account_ref,ctx.device_id)!=(original.actor_id,original.subject_id,original.account_ref,original.device_id):raise ValueError('request not owned')
                self._binding(original)
            except (TypeError,ValueError):return self._result(ctx,'REQUEST_ACCESS_DENIED',ok=False)
            try:
                request=self._request(original,row['operation'],json.loads(row['payload']),row['request_id'])
                self._event(ctx,'bank.query.requested',request_id=row['request_id'])
                response=self.transport.send('query_request',copy.deepcopy(request),5);self._apply(row,response)
                row=self._row(transaction_id);return self._result(ctx,'BANK_QUERY',self._data(row))
            except Exception:return self._result(ctx,'OUTCOME_UNKNOWN',self._data(row,uncertain=True))
    def query_by_idempotency(self,ctx,key):
        row=self.record_for_key(key)
        return self.query_transaction(ctx,row['request_id']) if row else self._result(ctx,'REQUEST_NOT_FOUND',ok=False)
    def reverse_transfer(self,ctx,transaction_id,reason):
        if not self.supports_reversal:return self._result(ctx,'REVERSAL_NOT_SUPPORTED',ok=False)
        with self.lock:
            checked=self.query_transaction(ctx,transaction_id)
            replay=self.record_for_key(ctx.idempotency_key)
            eligible=checked.data.get('status')=='succeeded' or checked.data.get('status')=='reversed' and replay is not None and replay['operation']=='reverse_transfer'
            if not checked.ok or not eligible or not isinstance(reason,str) or not 1<=len(reason)<=120:return self._result(ctx,'REVERSAL_NOT_ALLOWED',ok=False)
            return self._submit(ctx,'reverse_transfer',{k:checked.data[k] for k in ('amount_cents','currency','beneficiary_id')}|{'transaction_id':checked.data['provider_transaction_id'],'reason':reason})
    def apply_callback(self,raw,signature):
        with self.lock:
            try:
                if not isinstance(self.callback_secret,bytes) or len(self.callback_secret)<32 or not isinstance(raw,bytes) or len(raw)>32768 or not isinstance(signature,str):raise ValueError('callback disabled/invalid')
                if not hmac.compare_digest(hmac.new(self.callback_secret,raw,hashlib.sha256).hexdigest(),signature):raise ValueError('signature invalid')
                def pairs(items):
                    value={}
                    for k,v in items:
                        if k in value:raise ValueError('duplicate JSON key')
                        value[k]=v
                    return value
                event=json.loads(raw,object_pairs_hook=pairs)
                if not isinstance(event,dict) or set(event)!={'event_id','issued_at','receipt'} or not _id(event['event_id']) or type(event['issued_at']) is not int or abs(time.time()-event['issued_at'])>300:raise ValueError('stale callback')
                digest=hashlib.sha256(raw).hexdigest();old=self.db.execute('SELECT body_sha FROM bank_callback_events WHERE event_id=?',(event['event_id'],)).fetchone()
                if old:
                    if old[0]!=digest:raise ValueError('event replay content differs')
                    return {'ok':True,'duplicate':True,'business_executed':False}
                row=self._row(event['receipt']['request_id'])
                if row is None:raise ValueError('unowned callback')
                self._binding(AdapterContext(**json.loads(row['context'])))
                self._apply(row,event['receipt'])
                self.db.execute('INSERT INTO bank_callback_events VALUES(?,?,?)',(event['event_id'],digest,row['request_id']));self.db.commit()
                return {'ok':True,'duplicate':False,'business_executed':False}
            except Exception:return {'ok':False,'code':'CALLBACK_REJECTED','business_executed':False}
    def reconcile_statement(self,ctx):
        with self.lock:
            try:
                self._binding(ctx);self._event(ctx,'bank.statement.requested',account_ref=ctx.account_ref)
                response=self.transport.send('statement',self._request(ctx,'statement',{}),5)
                if not isinstance(response,dict) or set(response)!={'schema','subject_id','account_ref','items'} or response['schema']!=1 or response['subject_id']!=ctx.subject_id or response['account_ref']!=ctx.account_ref or not isinstance(response['items'],list) or len(response['items'])>1000:raise ValueError('invalid statement')
                rows=self.db.execute('SELECT * FROM bank_requests').fetchall();local={r['request_id']:r for r in rows if json.loads(r['context'])['account_ref']==ctx.account_ref and json.loads(r['context'])['actor_id']==ctx.actor_id}
                discrepancies=[];seen=set()
                for receipt in response['items']:
                    rid=receipt.get('request_id') if isinstance(receipt,dict) else None
                    if not isinstance(rid,str) or rid in seen:discrepancies.append({'request_id':rid if isinstance(rid,str) else None,'reason':'duplicate_or_invalid'});continue
                    seen.add(rid);row=local.get(rid)
                    if row is None:discrepancies.append({'request_id':rid,'reason':'bank_only'});continue
                    try:
                        original=AdapterContext(**json.loads(row['context']));self._binding(original)
                        state=self._validate(self._request(original,row['operation'],json.loads(row['payload']),rid),receipt)
                        prior=json.loads(row['receipt']) if row['receipt'] else None
                        if state!=row['status'] or receipt['sequence']<row['sequence']:discrepancies.append({'request_id':rid,'reason':'state_or_sequence_mismatch'})
                        elif prior and (prior['provider_request_id']!=receipt['provider_request_id'] or prior['payload'].get('transaction_id')!=receipt['payload'].get('transaction_id')):discrepancies.append({'request_id':rid,'reason':'provider_identity_mismatch'})
                    except (ValueError,TypeError,KeyError):discrepancies.append({'request_id':rid,'reason':'binding_or_amount_mismatch'})
                discrepancies.extend({'request_id':rid,'reason':'local_only'} for rid in local if rid not in seen)
                self._event(ctx,'bank.statement.compared',discrepancies=len(discrepancies))
                unresolved=sum(r['status'] not in {'succeeded','failed','reversed'} for r in local.values())
                return {'ok':True,'status':'MISMATCH' if discrepancies else 'PENDING_REVIEW' if unresolved else 'MATCHED','unresolved_count':unresolved,'discrepancies':discrepancies,'business_executed':False,'scope':'account-scoped request receipts, not full bank statement certification'}
            except Exception:return {'ok':False,'status':'UNAVAILABLE','discrepancies':[],'business_executed':False}


class ProtocolBankFacade:
    """Exercise the Agent chain against a local protocol fixture or pinned core demo.

    Base Mock keeps existing demo family/risk scenarios; it is NEVER a fallback
    source for service balance, daily limits, transfers or card operations.
    This facade cannot be enabled as a live/production bank core.
    """
    def __init__(self,base,service,result_type):
        if not isinstance(service,BankServiceAdapter) or getattr(service.transport,'scope',None) not in {'LOCAL_PROTOCOL_TEST','FINERACT_LOCAL_SIMULATOR'}:raise ValueError('only local protocol test transport supported')
        self._base=base;self.service=service;self.result_type=result_type;self.lock=base.lock
    def __getattr__(self,key):return getattr(self._base,key)
    def close(self):self.service.close();self._base.close()
    def _ctx(self,actor='demo-user',*,key='',trace=''):
        current=_ACTIVE.get()
        if current:
            if current.actor_id!=actor:raise BankServiceError('bank subject mismatch')
            from dataclasses import replace
            return replace(current,idempotency_key=key or current.idempotency_key,trace_id=trace or current.trace_id)
        return self.service.context(actor,trace_id=trace or 'bank-protocol-read',idempotency_key=key)
    def _tool(self,result,tool,**extra):
        return self.result_type(result.ok,tool,dict(result.data,**extra),None if result.ok else result.code,None if result.ok else result.message,result.trace_id)
    def _balance(self,actor):
        result=self.service.get_balance(self._ctx(actor))
        if not result.ok:raise BankServiceError('bank balance unavailable')
        return result
    @property
    def cards(self):return [] if self.service.service_scope=='FINERACT_LOCAL_SIMULATOR' else self._base.cards
    @property
    def balance_cents(self):return self.available_balance()
    @property
    def daily_transfer_cents(self):return self.daily_transfer_total_cents()
    def available_balance(self,actor_id='demo-user'):return self._balance(actor_id).data['available_cents']
    def daily_transfer_total_cents(self,actor_id='demo-user'):return self._balance(actor_id).data['daily_transfer_cents']
    def balance(self,actor_id='demo-user'):
        result=self.service.get_balance(self._ctx(actor_id))
        return self._tool(result,'get_balance',account_owner_id=actor_id,coverage={'complete':False,'scope':'fineract_synthetic_account_only' if self.service.service_scope=='FINERACT_LOCAL_SIMULATOR' else 'local_bank_protocol_fixture_only','sources':[{'source':'BankServiceAdapter','owner_id':actor_id}],'excluded':['real_bank','other_accounts','investments']})
    def check_transfer_constraints(self,amount_cents,actor_id='demo-user'):
        from policy_config import get_policy
        p=get_policy()
        if type(amount_cents) is not int or not 0<amount_cents<=p['max_amount_cents']:return self.result_type(False,'check_transfer_constraints',error_code='INVALID_AMOUNT',error_message='金额必须为正整数分。')
        try:balance=self._balance(actor_id).data
        except BankServiceError:return self.result_type(False,'check_transfer_constraints',error_code='BANK_READ_UNAVAILABLE',error_message='银行可用余额与限额暂不可核验。')
        if amount_cents>balance['available_cents'] or amount_cents+balance['daily_transfer_cents']>p['daily_transfer_limit_cents']:
            return self.result_type(False,'check_transfer_constraints',error_code='BANK_LIMIT',error_message='余额或平台日累计限额不足。')
        return self.result_type(True,'check_transfer_constraints',dict(balance,allowed=True,actor_id=actor_id,projected_daily_transfer_cents=amount_cents+balance['daily_transfer_cents']))
    def resolve_beneficiary(self,name):
        try:
            b=self.service._binding(self._ctx());ref=b.beneficiaries.get(name)
            if not ref:raise ValueError('beneficiary not in trusted mapping')
            return self.result_type(True,'resolve_beneficiary',{'name':name,'beneficiary_id':ref,'account_verified':True,'service_scope':self.service.service_scope})
        except (ValueError,BankServiceError):return self.result_type(False,'resolve_beneficiary',error_code='BENEFICIARY_NOT_FOUND',error_message='收款人未绑定银行协议演示标识。')
    def transfer(self,amount_cents,name,note,idempotency_key,actor_id='demo-user'):
        ctx=self._ctx(actor_id,key=idempotency_key);b=self.service._binding(ctx)
        result=self.service.create_transfer(ctx,amount_cents,b.beneficiaries.get(name,''),note)
        return self._tool(result,'create_transfer',beneficiary=name,actor_id=actor_id,note=note)
    def _row_for_idempotency(self,key):return self.service.record_for_key(key)
    def _row_for_transaction(self,transaction_id):
        with self.service.lock:
            row=self.service._row(transaction_id)
            return dict(row,actor_id=json.loads(row['context'])['actor_id'],transaction_id=row['request_id']) if row else None
    def query_transaction(self,transaction_id):
        row=self._row_for_transaction(transaction_id)
        if not row:return self.result_type(False,'query_transaction',error_code='TRANSACTION_NOT_FOUND',error_message='未找到原银行协议请求。')
        ctx=_ACTIVE.get() or AdapterContext(**json.loads(row['context']))
        result=self.service.query_transaction(ctx,transaction_id);data=dict(result.data)
        if row['operation']=='create_transfer':
            original=json.loads(row['payload']);b=self.service.resolve_binding(ctx.actor_id)
            data.update(actor_id=ctx.actor_id,beneficiary=next((name for name,ref in b.beneficiaries.items() if ref==original['beneficiary_id']),'已绑定收款人'),note=original.get('note',''))
        return self._tool(result,'query_transaction',**data)
    def recent_transactions(self,actor_id='demo-user'):
        with self.service.lock:
            self.service._binding(self._ctx(actor_id));items=[]
            for row in self.service.db.execute("SELECT * FROM bank_requests WHERE operation='create_transfer' ORDER BY updated_at DESC LIMIT 100"):
                context=json.loads(row['context']);payload=json.loads(row['payload'])
                if context['actor_id']==actor_id and row['status']=='succeeded':items.append({'id':row['request_id'],'category':'转账','merchant':'银行协议演示收款人','date':datetime.fromtimestamp(row['updated_at']).date().isoformat(),'amount_cents':-payload['amount_cents'],'status':'succeeded','actor_id':actor_id})
            return self.result_type(True,'list_transactions',{'items':items[:10],'account_owner_id':actor_id,'coverage':'locally journaled protocol requests only, not complete bank history'})
    def _card_operation(self,card_id,actor_id,key,trace,operation):
        ctx=self._ctx(actor_id,key=key,trace=trace);result=getattr(self.service,operation)(ctx,card_id);data=dict(result.data)
        state=data.get('status') if operation!='query_card' else 'succeeded'
        if operation in {'report_loss','unreport_loss'}:
            data['bank_request_status']=state
            if state in {'processing','unknown'}:data['status']=state
            else:data['status']='reported_loss' if data.get('card_status')=='reported_loss' else '正常'
        elif result.ok:data['status']='reported_loss' if data['card_status']=='reported_loss' else '正常'
        tool={'report_loss':'report_card_loss','unreport_loss':'unreport_card_loss','query_card':'verify_card_status'}[operation]
        return self._tool(result,tool,**data)
    def report_card_loss(self,card_id='card_001',actor_id='demo-user',idempotency_key='',trace_id=''):return self._card_operation(card_id,actor_id,idempotency_key,trace_id,'report_loss')
    def unreport_card_loss(self,card_id='card_001',actor_id='demo-user',idempotency_key='',trace_id=''):return self._card_operation(card_id,actor_id,idempotency_key,trace_id,'unreport_loss')
    def card_status_query(self,card_id='card_001',actor_id='demo-user',trace_id=''):return self._card_operation(card_id,actor_id,'',trace_id,'query_card')
    def cards_query(self):
        ctx=self._ctx();b=self.service._binding(ctx);items=[]
        if self.service.service_scope=='FINERACT_LOCAL_SIMULATOR':
            verified=self.service.get_balance(ctx)
            if not verified.ok:return self._tool(verified,'list_cards')
            return self.result_type(True,'list_cards',{'items':[], 'service_scope':self.service.service_scope,
                'payment_rail':'savings_account_transfer','account_active':True,'account_owner_id':ctx.subject_id,
                'account_ref':ctx.account_ref,'card_management_supported':False})
        for card in b.card_ids:
            result=self.card_status_query(card,ctx.actor_id)
            if not result.ok:return result
            items.append(dict(result.data,id=card,name='银行协议演示卡',last4='0001',status='挂失' if result.data['status']=='reported_loss' else '正常'))
        return self.result_type(True,'list_cards',{'items':items,'service_scope':self.service.service_scope})
