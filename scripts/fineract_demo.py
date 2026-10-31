#!/usr/bin/env python3
"""Pinned local Apache Fineract demo lifecycle; synthetic funds, no live bank.

start uses cached approved images only. seed/serve do not require Docker access
and can run inside the agent image sharing the core's network namespace.
No commit/push, global Docker config, image pull or automatic business retry.
"""
import argparse,base64,hashlib,json,os,secrets,string,subprocess,sys,time,uuid
import urllib.request
from pathlib import Path
from datetime import datetime,timezone,timedelta
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'app'))
from fineract_transport import FineractAPI,FineractTransport,cents
APP='apache/fineract@sha256:0cd33902744e0e527e52fd905f65d67b706a35c40085fb21034653e0b2064aa4'
DB='postgres@sha256:a145910d7079e9fbf73e6df19d5fcca0ce59d747cf7d97ac772bff28c3759c32'
COMMIT='ad98847389cd72e47694fc96af609714449e666f'
def docker(*args,timeout=120):
    r=subprocess.run(['docker',*args],stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=timeout)
    if r.returncode:raise RuntimeError('Docker operation failed; inspect this demo project locally')
    return r.stdout.decode('utf-8',errors='replace')
def owned_folder(path):
    path=path.resolve()
    container_volume=os.name!='nt' and os.getenv('BANK_AGENT_CONTAINER_DEMO')=='1' and path.is_relative_to(Path('/data'))
    if not container_volume and (not path.is_relative_to(ROOT) or path==ROOT):raise ValueError('demo folder must be inside workspace')
    path.mkdir(parents=True,exist_ok=True)
    return path

RELAY="from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer\nimport http.client,os\nimport json,hashlib,time\nfrom pathlib import Path\nclass Handler(BaseHTTPRequestHandler):\n def log_message(self,*args):pass\n def do_GET(self):self.proxy()\n def do_POST(self):\n  if self.path=='/hooks/__CALLBACK_NONCE__':\n   size=int(self.headers.get('Content-Length','0'))\n   if not 0<size<=65536:self.send_error(413);return\n   raw=self.rfile.read(size)\n   record={'time':time.time(),'body_sha256':hashlib.sha256(raw).hexdigest(),'bytes':len(raw),'entity':self.headers.get('X-Fineract-Entity'),'action':self.headers.get('X-Fineract-Action'),'signature_present':bool(self.headers.get('X-Fineract-Signature')),'trusted_bank_fact':False}\n   with Path('/data/callbacks.jsonl').open('a') as f:f.write(json.dumps(record)+'\\n')\n   self.send_response(200);self.send_header('Content-Length','2');self.end_headers();self.wfile.write(b'{}');return\n  self.proxy()\n def do_PUT(self):self.proxy()\n def do_DELETE(self):self.proxy()\n def proxy(self):\n  client=http.client.HTTPConnection('fineract',int(os.getenv('TARGET_PORT','8443')),timeout=60)\n  try:\n   size=int(self.headers.get('Content-Length','0'))\n   if not 0<=size<=2097152:raise ValueError('request size')\n   body=self.rfile.read(size)\n   headers={k:v for k,v in self.headers.items() if k.lower() not in {'host','connection','transfer-encoding','content-length'}}\n   client.request(self.command,self.path,body,headers);response=client.getresponse();data=response.read(16777217)\n   if len(data)>16777216:raise ValueError('response size')\n   self.send_response(response.status)\n   for k,v in response.getheaders():\n    if k.lower() not in {'connection','transfer-encoding','content-length'}:self.send_header(k,v)\n   self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)\n  except (OSError,ValueError):\n   self.send_response(502);self.send_header('Content-Length','0');self.end_headers()\n  finally:client.close()\nThreadingHTTPServer(('0.0.0.0',8080),Handler).serve_forever()\n"

def start(work,project):
    out=work.parent
    private=work/'private.json'
    if private.exists():config=json.loads(private.read_text(encoding='utf-8'))
    else:
     config={'db_password':secrets.token_hex(24),'master_password':secrets.token_hex(24)}
     private.write_text(json.dumps(config),encoding='utf-8')
    config.setdefault('callback_nonce',secrets.token_hex(24));private.write_text(json.dumps(config),encoding='utf-8');(work/'callback-data').mkdir(exist_ok=True)
    (work/'init.sql').write_text('CREATE DATABASE fineract_tenants;\nCREATE DATABASE fineract_default;\n',encoding='ascii')
    relay=RELAY.replace('__CALLBACK_NONCE__',config['callback_nonce'])
    (work/'relay.py').write_text(relay,encoding='utf-8')
    env={'FINERACT_HIKARI_DRIVER_SOURCE_CLASS_NAME':'org.postgresql.Driver','FINERACT_HIKARI_JDBC_URL':'jdbc:postgresql://db:5432/fineract_tenants',
     'FINERACT_HIKARI_USERNAME':'postgres','FINERACT_HIKARI_PASSWORD':config['db_password'],
     'FINERACT_DEFAULT_TENANTDB_HOSTNAME':'db','FINERACT_DEFAULT_TENANTDB_PORT':'5432','FINERACT_DEFAULT_TENANTDB_UID':'postgres','FINERACT_DEFAULT_TENANTDB_PWD':config['db_password'],
     'FINERACT_DEFAULT_TENANTDB_IDENTIFIER':'default','FINERACT_DEFAULT_TENANTDB_NAME':'fineract_default','FINERACT_DEFAULT_TENANTDB_TIMEZONE':'Asia/Shanghai',
     'FINERACT_DEFAULT_MASTER_PASSWORD':config['master_password'],'FINERACT_SERVER_SSL_ENABLED':'false','SERVER_PORT':'8443',
     'SPRING_PROFILES_ACTIVE':'test','FINERACT_REMOTE_JOB_MESSAGE_HANDLER_SPRING_EVENTS_ENABLED':'true','FINERACT_EXTERNAL_EVENTS_ENABLED':'false',
     'FINERACT_CONTENT_FILESYSTEM_ROOT_FOLDER':'/tmp','FINERACT_HIKARI_MINIMUM_IDLE':'1','FINERACT_HIKARI_MAXIMUM_POOL_SIZE':'5',
     'JAVA_TOOL_OPTIONS':'-Xmx1024m -XX:TieredStopAtLevel=1 -XX:+UseContainerSupport --add-opens=java.base/java.lang=ALL-UNNAMED --add-opens=java.base/java.util=ALL-UNNAMED'}
    compose={'services':{
     'db':{'image':DB,'environment':{'POSTGRES_PASSWORD':config['db_password']},'volumes':['bank-db:/var/lib/postgresql',{'type':'bind','source':str(work/'init.sql'),'target':'/docker-entrypoint-initdb.d/init.sql','read_only':True}],
           'networks':['internal'],'mem_limit':'512m','healthcheck':{'test':['CMD-SHELL','pg_isready -U postgres'],'interval':'5s','timeout':'3s','retries':20}},
     'fineract':{'image':APP,'environment':env,'depends_on':{'db':{'condition':'service_healthy'}},'networks':['internal'],'mem_limit':'1536m','cap_drop':['ALL'],'security_opt':['no-new-privileges:true']},
     'relay':{'image':'python:3.12-slim','command':['python','/relay.py'],'volumes':[{'type':'bind','source':str(work/'relay.py'),'target':'/relay.py','read_only':True}],
              'ports':['127.0.0.1::8080'],'networks':['internal','frontend'],'mem_limit':'96m','read_only':True,'tmpfs':['/tmp'],'cap_drop':['ALL'],'security_opt':['no-new-privileges:true']}
     },'volumes':{'bank-db':{}},'networks':{'internal':{'internal':True},'frontend':{}}}
    compose['services']['relay']['volumes'].append({'type':'bind','source':str(work/'callback-data'),'target':'/data'})
    (work/'compose.json').write_text(json.dumps(compose,indent=2),encoding='utf-8');command=['compose','-p',project,'-f',str(work/'compose.json')]
    docker(*command,'up','-d','--pull','never');print('Fineract isolated containers started',flush=True)
    cid=docker(*command,'ps','-q','relay').strip();info=json.loads(docker('inspect',cid))[0];port=info['NetworkSettings']['Ports']['8080/tcp'][0]['HostPort'];base='http://127.0.0.1:'+port+'/fineract-provider'
    config['base_url']=base;private.write_text(json.dumps(config),encoding='utf-8');opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline=time.monotonic()+420
    while time.monotonic()<deadline:
     try:
      with opener.open(base+'/actuator/health',timeout=3) as response:status=json.load(response)
      if status.get('status')=='UP':print('FINERACT_HEALTH_UP',flush=True);break
     except Exception:pass
     time.sleep(3)
    else:
     logs=docker(*command,'logs','--tail','120','fineract');logs=logs.replace(config['db_password'],'[redacted]').replace(config['master_password'],'[redacted]')
     (out/'startup-failure.log').write_text(logs,encoding='utf-8');print(logs[-6000:]);raise SystemExit(1)
    headers={'Authorization':'Basic '+base64.b64encode((config.get('username','mifos')+':'+config.get('password','password')).encode()).decode(),'Fineract-Platform-TenantId':'default'}
    request=urllib.request.Request(base+'/api/v1/offices',headers=headers)
    with opener.open(request,timeout=20) as response:offices=json.load(response)
    (out/'startup.json').write_text(json.dumps({'status':'PASS','health':status,'offices':offices,'scope':'self-hosted open-source core, synthetic only','images':{'app':APP,'database':DB},'project':project},indent=2),encoding='utf-8')
    print('Fineract authenticated local API ready; no real bank connection',flush=True)
    return config


def runtime(work):
    os.environ['BANK_AGENT_OFFLINE']='1';os.environ['BANK_AGENT_DB_PATH']=str(work/'import.sqlite3')
    import banking_agent as ba
    from bank_service import BankBinding,BankServiceAdapter
    config=json.loads((work/'private.json').read_text(encoding='utf-8'))
    api=FineractAPI(config['base_url'],config['username'],config['password'])
    transport=FineractTransport(work/'transport.sqlite3',api,{'account_id':config['source_account_id'],'client_id':config['source_client_id'],
        'payment_type_id':config['payment_type_id'],'beneficiaries':{'benef-mom':{'account_id':config['target_account_id'],'client_id':config['target_client_id']}}})
    binding=BankBinding('demo-user','demo-user','fineract-account:'+str(config['source_account_id']),'local-core-grant',1,{'妈妈':'benef-mom'},())
    service=BankServiceAdapter(work/'requests.sqlite3',transport,lambda actor:binding if actor=='demo-user' else None,lambda *args:None,supports_reversal=True)
    return ba,ba.AgentRuntime(db_path=work/'agent.sqlite3',offline=True,bank_service=service),api,config


def register_hook(work):
    config=json.loads((work/'private.json').read_text(encoding='utf-8'))
    if config.get('hook_id'):return
    api=FineractAPI(config['base_url'],config['username'],config['password'])
    reply=api('POST','/hooks',{'name':'Web','displayName':'Competition internal core callback','isActive':True,
        'events':[{'entityName':'ACCOUNTTRANSFER','actionName':'CREATE'},{'entityName':'ACCOUNTTRANSFER','actionName':'UNDO'}],
        'config':{'Payload URL':'http://relay:8080/hooks/'+config['callback_nonce'],'Content Type':'json'}},'seed-native-hook')
    config['hook_id']=reply['resourceId'];(work/'private.json').write_text(json.dumps(config,indent=2),encoding='utf-8')


def qa(work,record):
    register_hook(work);ba,rt,api,config=runtime(work);service=rt.bank_service
    report={'status':'FAIL','scope':'actual pinned self-hosted open-source core; synthetic CNY, not a real bank','checks':{}}
    def check(name,value):
        report['checks'][name]=bool(value)
        if not value:raise AssertionError(name)
    def balance(role):return cents(api('GET','/savingsaccounts/'+str(config[role+'_account_id']))['summary']['availableBalance'])
    try:
        started=time.time();source_before,target_before=balance('source'),balance('target')
        plan=rt.chat('给妈妈转1元');check('confirmed_plan_required',plan.get('ok') and plan.get('plan_view',{}).get('requires_confirmation'))
        check('no_debit_before_confirmation',balance('source')==source_before)
        result=rt.confirm(plan['plan']['plan_id'],plan['plan_view']['action_token']);check('original_chain_success',result.get('result',{}).get('status')=='SUCCESS')
        data=next(item['data'] for item in result['result']['results'] if item['tool']=='create_transfer')
        check('core_source_debit_100',balance('source')==source_before-100);check('core_target_credit_100',balance('target')==target_before+100)
        check('duplicate_confirmation_denied',not rt.confirm(plan['plan']['plan_id'],plan['plan_view']['action_token'])['ok'])
        check('no_duplicate_debit',balance('source')==source_before-100)
        check('local_and_core_ids_distinct',data['transaction_id'].startswith('bankreq_') and data['provider_transaction_id'].startswith('fineract-transfer-'))
        check('query_original_core_status',rt.query_transaction(data['transaction_id'])['status']=='succeeded')
        ctx=service.context('demo-user',trace_id='trace-explicit-undo-'+uuid.uuid4().hex.translate(str.maketrans('0123456789','ghijklmnop')),
            idempotency_key='undo-'+data['transaction_id'],operation_digest=hashlib.sha256(('explicit synthetic undo '+data['provider_transaction_id']).encode()).hexdigest())
        check('request_reconciliation_before_undo',service.reconcile_statement(ctx)['status']=='MATCHED')
        check('unsupported_cards_not_fabricated',rt.bank.cards_query().data.get('card_management_supported') is False)
        today=datetime.now(timezone(timedelta(hours=8))).date().isoformat();native_key='native-'+uuid.uuid4().hex
        withdrawal=api('POST',f"/savingsaccounts/{config['source_account_id']}/transactions?command=withdrawal",{'transactionDate':today,'transactionAmount':'1.25','paymentTypeId':config['payment_type_id'],'dateFormat':'yyyy-MM-dd','locale':'en'},native_key)
        check('native_withdrawal_125',balance('source')==source_before-225)
        native_id=withdrawal['resourceId'];api('POST',f"/savingsaccounts/{config['source_account_id']}/transactions/{native_id}?command=undo",{},native_key+'-undo')
        native=api('GET',f"/savingsaccounts/{config['source_account_id']}/transactions/{native_id}")
        check('native_undo_balance_restored',balance('source')==source_before-100);check('native_original_marked_reversed',native.get('reversed') is True)
        undo=service.reverse_transfer(ctx,data['transaction_id'],'explicit synthetic core QA')
        check('core_transfer_undo_verified',undo.ok and undo.data.get('status')=='reversed')
        check('payer_restored',balance('source')==source_before);check('payee_restored',balance('target')==target_before)
        replay=service.reverse_transfer(ctx,data['transaction_id'],'explicit synthetic core QA')
        check('reversal_replay_uses_existing_receipt',replay.ok and replay.data.get('idempotent_replay') is True)
        check('no_second_refund_or_payee_debit',balance('source')==source_before and balance('target')==target_before)
        check('original_query_reversed',service.query_transaction(ctx,data['transaction_id']).data.get('status')=='reversed')
        check('request_reconciliation_after_undo',service.reconcile_statement(ctx)['status']=='MATCHED')
        path=work/'callback-data/callbacks.jsonl';callbacks=[]
        for _ in range(20):
            callbacks=[e for e in (json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()) if e['time']>=started] if path.exists() else []
            if any(e.get('action')=='UNDO' for e in callbacks):break
            time.sleep(.5)
        check('actual_native_callback_received',any(e.get('entity')=='ACCOUNTTRANSFER' and e.get('action')=='UNDO' for e in callbacks))
        check('callbacks_not_treated_as_bank_facts',all(e.get('trusted_bank_fact') is False for e in callbacks))
        check('original_audit_valid',rt.audit.verify_trace(plan['trace_id'])['valid']);check('undo_audit_valid',rt.audit.verify_trace(ctx.trace_id)['valid'])
        report.update(status='PASS',transfer={'local_reference':data['transaction_id'],'core_reference':data['provider_transaction_id'],'amount_cents':100},
            source_before_cents=source_before,source_after_cents=balance('source'),target_before_cents=target_before,target_after_cents=balance('target'),
            native_withdrawal_undo_id=native_id,transfer_undo_status=undo.data['status'],callbacks=callbacks,provider_signature_verified=False,
            formal_bank_statement_verified=False,scope_limits=['self-hosted synthetic core, not bank-issued credentials or certification','native Web hook is an untrusted hint; authenticated re-query is the fact source','request-level transfer reconciliation, not a licensed bank statement','card management unsupported'])
    except Exception as exc:report.update(error_type=type(exc).__name__,error=str(exc),validation=api.last_error)
    finally:
        record.parent.mkdir(parents=True,exist_ok=True);record.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
        for item in (rt,ba.runtime):item.recovery_scheduler.stop();item.bank.close();item.audit.db.close()
    print(json.dumps({'status':report['status'],'checks':len(report['checks']),'record':str(record)}));return report['status']=='PASS'


def serve(work,host,port):
    if host=='0.0.0.0' and os.getenv('BANK_AGENT_CONTAINER_DEMO')!='1':raise ValueError('host demo must bind literal loopback')
    ba,rt,api,config=runtime(work);original=ba.runtime;ba.runtime=rt;ba.gateway=ba.AgentGateway(rt)
    server=ba.ThreadingHTTPServer((host,port),ba.Handler)
    print('Self-hosted Fineract synthetic bank demo listening on '+host+':'+str(server.server_port),flush=True)
    try:server.serve_forever()
    except KeyboardInterrupt:pass
    finally:
        server.server_close()
        for item in (rt,original):item.recovery_scheduler.stop();item.bank.close();item.audit.db.close()


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('action',choices=['start','seed','qa','serve','stop'])
    parser.add_argument('--directory',type=Path,default=ROOT/'.qa-tools/fineract-demo');parser.add_argument('--project',default='bank-fineract-demo')
    parser.add_argument('--base-url',default='http://127.0.0.1:8443/fineract-provider');parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8091);parser.add_argument('--record',type=Path)
    args=parser.parse_args();work=owned_folder(args.directory)
    if not args.project.replace('-','').isalnum():raise ValueError('invalid owned Compose project name')
    if args.action=='start':start(work,args.project)
    elif args.action=='seed':
        private=work/'private.json'
        if not private.exists():private.write_text(json.dumps({'base_url':args.base_url,'callback_nonce':os.getenv('FINERACT_DEMO_CALLBACK_NONCE') or secrets.token_hex(24)}),encoding='utf-8')
        deadline=time.monotonic()+420
        while True:
            c=json.loads(private.read_text(encoding='utf-8'));api=FineractAPI(c['base_url'],c.get('username','mifos'),c.get('password','password'))
            try:api('GET','/offices');break
            except Exception:
                if time.monotonic()>deadline:raise
                time.sleep(3)
        seed(work);register_hook(work)
    elif args.action=='qa':
        record=args.record or work/'core-qa.json'
        if not record.resolve().is_relative_to(ROOT) and not (os.getenv('BANK_AGENT_CONTAINER_DEMO')=='1' and record.resolve().is_relative_to(work)):raise ValueError('record must stay in workspace/demo volume')
        return 0 if qa(work,record) else 1
    elif args.action=='serve':serve(work,args.host,args.port)
    else:
        # Stop only. Keep this project's named data volumes; no global prune.
        docker('compose','-p',args.project,'-f',str(work/'compose.json'),'stop')
    return 0




def seed(work):
    out=work.parent
    p=work/'private.json';config=json.loads(p.read_text(encoding='utf-8'));today=datetime.now(timezone(timedelta(hours=8))).date().isoformat()
    def save():p.write_text(json.dumps(config,indent=2),encoding='utf-8')
    if 'password' not in config:
        new='Demo!'
        for _ in range(24):new+=secrets.choice(''.join(c for c in string.ascii_letters+string.digits if c!=new[-1]))
        new+='X9' if new[-1]!='X' else 'Y9';config['next_admin_password']=new;save()
        api=FineractAPI(config['base_url'],'mifos','password');users=api('GET','/users');user=next(u for u in users if u['username']=='mifos')
        api('PUT','/users/'+str(user['id']),{'password':new,'repeatPassword':new})
        config.update(username='mifos',password=new);config.pop('next_admin_password',None);save();print('Local demo administrator password rotated',flush=True)
    api=FineractAPI(config['base_url'],config['username'],config['password']);api('PUT','/currencies',{'currencies':['CNY']})
    if 'product_id' not in config:
        product={'name':'Competition CNY Savings','shortName':'P07C','description':'Synthetic competition accounts, not a real bank',
           'currencyCode':'CNY','digitsAfterDecimal':2,'inMultiplesOf':1,'nominalAnnualInterestRate':0,'interestCompoundingPeriodType':1,
           'interestPostingPeriodType':4,'interestCalculationType':1,'interestCalculationDaysInYearType':365,'accountingRule':1,'withdrawalFeeForTransfers':False,'locale':'en'}
        config['product_id']=api('POST','/savingsproducts',product,'seed-product')['resourceId'];save();print('Savings product created',flush=True)
    for role,first,last in [('source','Contest','User'),('target','Contest','Beneficiary')]:
        client=role+'_client_id';account=role+'_account_id'
        if client not in config:
            config[client]=api('POST','/clients',{'officeId':1,'legalFormId':1,'firstname':first,'lastname':last,'active':True,'activationDate':today,'dateFormat':'yyyy-MM-dd','locale':'en'},'seed-'+role+'-client-v2')['resourceId'];save()
        if account not in config:
            config[account]=api('POST','/savingsaccounts',{'clientId':config[client],'productId':config['product_id'],'submittedOnDate':today,'dateFormat':'yyyy-MM-dd','locale':'en'},'seed-'+role+'-account')['resourceId'];save()
        current=api('GET','/savingsaccounts/'+str(config[account]))
        if current['status'].get('submittedAndPendingApproval'):
            api('POST',f'/savingsaccounts/{config[account]}?command=approve',{'approvedOnDate':today,'dateFormat':'yyyy-MM-dd','locale':'en'},'seed-'+role+'-approve')
        current=api('GET','/savingsaccounts/'+str(config[account]))
        if current['status'].get('approved'):
            api('POST',f'/savingsaccounts/{config[account]}?command=activate',{'activatedOnDate':today,'dateFormat':'yyyy-MM-dd','locale':'en'},'seed-'+role+'-activate')
        print('Synthetic '+role+' client/account active',flush=True)
    if 'payment_type_id' not in config:
        types=api('GET','/paymenttypes');config['payment_type_id']=types[0]['id'];save()
    if 'seed_deposit_id' not in config:
        config['seed_deposit_id']=api('POST',f"/savingsaccounts/{config['source_account_id']}/transactions?command=deposit",{'transactionDate':today,'transactionAmount':'100000.00','paymentTypeId':config['payment_type_id'],'dateFormat':'yyyy-MM-dd','locale':'en'},'seed-source-deposit-v2')['resourceId'];save()
    source=api('GET',f"/savingsaccounts/{config['source_account_id']}?associations=transactions")
    target=api('GET',f"/savingsaccounts/{config['target_account_id']}?associations=transactions")
    transfers=api('GET','/accounttransfers?offset=0&limit=100');hook=api('GET','/hooks/template')
    def default(v):
        from decimal import Decimal
        return str(v) if isinstance(v,Decimal) else v
    for name,data in [('source-account',source),('target-account',target),('initial-transfer-list',transfers),('hook-template',hook)]:
        (out/(name+'.json')).write_text(json.dumps(data,ensure_ascii=False,indent=2,default=default),encoding='utf-8')
    print(json.dumps({'seed':'PASS','source_id':config['source_account_id'],'target_id':config['target_account_id'],'source_summary':source['summary'],'transfer_list_keys':list(transfers) if isinstance(transfers,dict) else type(transfers).__name__,'hook_template_type':type(hook).__name__},default=default))

    return config


if __name__=='__main__':raise SystemExit(main())
