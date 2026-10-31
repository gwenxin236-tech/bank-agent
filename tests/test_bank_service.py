"""P07 local normalized bank-service conformance. Never a real banking endpoint."""
import copy,hashlib,hmac,json,os,sys,tempfile,threading,time,unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from datetime import datetime,timezone
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
from bank_service import BankServiceAdapter, BankBinding, BankServiceError
from production_contracts import AdapterContext
import banking_agent as ba


class ProtocolFixture:
    """Independent in-memory service: normalized profile owned by this project."""
    scope='LOCAL_PROTOCOL_TEST'
    def __init__(self):
        self.calls=[];self.writes=0;self.records={};self.balance=10000000;self.daily=0;self.card_status='normal';self.fault=None;self.submit_state='succeeded'
    def send(self,operation,body,timeout):
        self.calls.append((operation,copy.deepcopy(body)));assert 0<timeout<=10
        if self.fault=='query_down' and operation=='query_request':raise TimeoutError('sensitive gateway detail')
        if operation=='query_request':return copy.deepcopy(self.records[body['request_id']])
        if operation=='statement':return {'schema':1,'subject_id':body['subject_id'],'account_ref':body['account_ref'],'items':[copy.deepcopy(r) for r in self.records.values() if r['operation'] in {'create_transfer','reverse_transfer','report_loss','unreport_loss'}]}
        payload={}
        if operation=='get_balance':payload={'available_cents':self.balance,'daily_transfer_cents':self.daily,'currency':'CNY','as_of':datetime.now(timezone.utc).isoformat()}
        elif operation=='query_card':payload={'card_id':body['payload']['card_id'],'card_status':self.card_status}
        else:
            self.writes+=1
            if operation=='create_transfer':
                payload=dict(body['payload'],transaction_id='provider-tx-'+str(self.writes));payload.pop('note',None)
                if self.submit_state=='succeeded':self.balance-=payload['amount_cents'];self.daily+=payload['amount_cents']
            elif operation in {'report_loss','unreport_loss'}:
                if self.submit_state=='succeeded':self.card_status='reported_loss' if operation=='report_loss' else 'normal'
                payload={'card_id':body['payload']['card_id'],'card_status':self.card_status}
            elif operation=='reverse_transfer':payload=dict(body['payload']);payload.pop('reason',None)
        reply={k:body[k] for k in ('schema','operation','request_id','actor_id','subject_id','account_ref','idempotency_key')}
        reply.update(provider_request_id='provider-'+body['request_id'],status=self.submit_state if operation not in {'get_balance','query_card'} else 'succeeded',sequence=1,payload=payload)
        self.records[body['request_id']]=copy.deepcopy(reply)
        if self.fault=='lost_response' and operation not in {'get_balance','query_card'}:raise TimeoutError('sensitive gateway detail')
        if self.fault=='malformed':reply['payload']['amount_cents']=True
        if self.fault=='wrong_account':reply['account_ref']='foreign-account'
        return reply


class BankServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'bank.sqlite3';self.transport=ProtocolFixture();self.events=[]
        self.binding=BankBinding('demo-user','demo-user','acct-demo','grant-demo',1,{'妈妈':'benef-mom'},('card_001',))
        self.ctx=AdapterContext('trace-p07','demo-user',idempotency_key='key-p07',subject_id='demo-user',account_ref='acct-demo',authorization_ref='grant-demo',authorization_version=1,operation_digest='a'*64)
        self.adapter=self.make()
    def make(self):return BankServiceAdapter(self.path,self.transport,lambda actor:self.binding if actor=='demo-user' else None,self.audit,callback_secret=b'fixture-only-'*4)
    def audit(self,ctx,event,data):self.events.append((ctx.trace_id,event,copy.deepcopy(data)))
    def tearDown(self):self.adapter.close();self.tmp.cleanup()
    def transfer(self,ctx=None,amount=100):return self.adapter.create_transfer(ctx or self.ctx,amount,'benef-mom','test')
    def test_context_binding_and_strict_amount_reject_before_transport(self):
        for ctx in [replace(self.ctx,actor_id='other'),replace(self.ctx,device_id='other-device'),replace(self.ctx,subject_id='other'),replace(self.ctx,account_ref='foreign'),replace(self.ctx,authorization_ref='forged'),replace(self.ctx,authorization_version=2),replace(self.ctx,operation_digest=''),replace(self.ctx,idempotency_key='')]:
            self.assertFalse(self.transfer(ctx).ok)
        for amount in [True,0,-1,1.2,'100']:self.assertFalse(self.transfer(amount=amount).ok)
        self.assertEqual(self.transport.writes,0)
    def test_persistent_idempotency_and_payload_conflict(self):
        first=self.transfer();self.assertTrue(first.ok);self.assertEqual(first.data['status'],'succeeded')
        self.assertTrue(self.transfer().data['idempotent_replay']);self.assertFalse(self.transfer(amount=101).ok)
        self.adapter.close();self.adapter=self.make();self.assertTrue(self.transfer().data['idempotent_replay']);self.assertEqual(self.transport.writes,1)
    def test_lost_reply_is_unknown_and_read_only_query_recovers_same_request(self):
        self.transport.fault='lost_response';r=self.transfer();self.assertTrue(r.ok);self.assertTrue(r.data['outcome_unknown'])
        self.assertNotIn('sensitive',json.dumps(r.data));self.assertTrue(self.transfer().data['idempotent_replay']);self.assertEqual(self.transport.writes,1)
        self.transport.fault=None;q=self.adapter.query_transaction(self.ctx,r.data['transaction_id']);self.assertEqual(q.data['status'],'succeeded');self.assertEqual(self.transport.writes,1)
    def test_processing_and_failed_queries_never_become_bank_failure(self):
        self.transport.submit_state='accepted';r=self.transfer();self.assertEqual(r.data['status'],'processing')
        self.transport.fault='query_down';q=self.adapter.query_transaction(self.ctx,r.data['transaction_id']);self.assertEqual(q.data['status'],'unknown');self.assertTrue(q.ok)
        self.assertEqual(self.transport.writes,1)
    def test_wrong_account_malformed_receipt_and_unowned_query_fail_closed(self):
        for fault in ['wrong_account','malformed']:
            self.transport.fault=fault;r=self.transfer(replace(self.ctx,idempotency_key=fault));self.assertEqual(r.data['status'],'unknown')
        calls=len(self.transport.calls);self.assertFalse(self.adapter.query_transaction(replace(self.ctx,actor_id='other'),'unknown').ok);self.assertEqual(len(self.transport.calls),calls)
    def test_audit_or_persistent_journal_failure_prevents_dispatch(self):
        with patch.object(self.adapter,'audit',side_effect=OSError('audit unavailable')):self.assertFalse(self.transfer().ok)
        self.assertEqual(self.transport.writes,0)
        with patch.object(self.adapter,'_reserve',side_effect=OSError('disk unavailable')):self.assertFalse(self.transfer().ok)
        self.assertEqual(self.transport.writes,0)
    def test_concurrent_same_key_submits_once(self):
        results=[];threads=[threading.Thread(target=lambda:results.append(self.transfer())) for _ in range(8)]
        for t in threads:t.start()
        for t in threads:t.join()
        self.assertEqual(self.transport.writes,1);self.assertEqual(len(results),8);self.assertTrue(all(r.ok for r in results))
    def test_balance_units_time_and_card_status_use_service_results(self):
        b=self.adapter.get_balance(self.ctx);self.assertEqual(b.data['available_cents'],10000000);self.assertEqual(b.data['currency'],'CNY');self.assertIn('as_of',b.data)
        r=self.adapter.report_loss(self.ctx,'card_001');self.assertEqual(r.data['status'],'succeeded')
        q=self.adapter.query_card(self.ctx,'card_001');self.assertEqual(q.data['card_status'],'reported_loss')
        self.assertFalse(self.adapter.report_loss(replace(self.ctx,idempotency_key='foreign-card'),'card-other').ok)
    def test_reversal_is_explicit_unsupported_by_default(self):
        r=self.transfer();calls=len(self.transport.calls)
        result=self.adapter.reverse_transfer(self.ctx,r.data['transaction_id'],'test')
        self.assertFalse(result.ok);self.assertEqual(result.code,'REVERSAL_NOT_SUPPORTED');self.assertEqual(len(self.transport.calls),calls)
        self.adapter.supports_reversal=True
        ctx=replace(self.ctx,idempotency_key='explicit-reversal')
        reversed_request=self.adapter.reverse_transfer(ctx,r.data['transaction_id'],'explicit protocol test')
        self.assertTrue(reversed_request.ok);self.assertEqual(self.transport.writes,2)
        self.assertTrue(self.adapter.reverse_transfer(ctx,r.data['transaction_id'],'explicit protocol test').data['idempotent_replay']);self.assertEqual(self.transport.writes,2)
    def test_callback_signature_binding_duplicates_and_order(self):
        self.transport.submit_state='processing';r=self.transfer();request_id=r.data['transaction_id'];body=copy.deepcopy(self.transport.records[request_id])
        body.update(status='succeeded',sequence=2);event={'event_id':'event-one','issued_at':int(time.time()),'receipt':body}
        def send(value,signature=None):
            raw=json.dumps(value,sort_keys=True).encode();sig=signature or hmac.new(b'fixture-only-'*4,raw,hashlib.sha256).hexdigest()
            return self.adapter.apply_callback(raw,sig)
        self.assertFalse(send(event,'0'*64)['ok']);self.assertTrue(send(event)['ok']);self.assertTrue(send(event)['duplicate'])
        altered=copy.deepcopy(event);altered['receipt']['status']='failed';self.assertFalse(send(altered)['ok'])
        stale=copy.deepcopy(event);stale['event_id']='event-stale';stale['receipt']['sequence']=1;self.assertFalse(send(stale)['ok'])
        foreign=copy.deepcopy(event);foreign['event_id']='event-foreign';foreign['receipt']['sequence']=3;foreign['receipt']['account_ref']='other';self.assertFalse(send(foreign)['ok'])
        expired=copy.deepcopy(event);expired['issued_at']-=600;self.assertFalse(send(expired)['ok'])
        regressed=copy.deepcopy(event);regressed['event_id']='event-regression';regressed['receipt'].update(sequence=3,status='failed')
        self.assertFalse(send(regressed)['ok']);self.assertEqual(self.adapter._row(request_id)['status'],'succeeded')
    def test_statement_reconciliation_reports_mismatch_without_funds_action(self):
        self.transfer();before=self.transport.writes;clean=self.adapter.reconcile_statement(self.ctx);self.assertEqual(clean['status'],'MATCHED')
        row=next(iter(self.transport.records.values()));row['payload']['amount_cents']+=1
        mismatch=self.adapter.reconcile_statement(self.ctx);self.assertEqual(mismatch['status'],'MISMATCH');self.assertEqual(self.transport.writes,before)

    def test_stale_balance_and_revoked_authorization_are_not_facts(self):
        original=self.transport.send
        def stale(operation,body,timeout):
            reply=original(operation,body,timeout)
            if operation=='get_balance':reply['payload']['as_of']='2020-01-01T00:00:00+00:00'
            return reply
        with patch.object(self.transport,'send',side_effect=stale):self.assertFalse(self.adapter.get_balance(self.ctx).ok)
        self.binding=replace(self.binding,authorization_version=2)
        self.assertFalse(self.transfer().ok);self.assertEqual(self.transport.writes,0)

    def test_failed_terminal_state_and_pending_reconciliation_remain_distinct(self):
        self.transport.submit_state='failed';r=self.transfer();self.assertFalse(r.ok)
        q=self.adapter.query_transaction(self.ctx,r.data['transaction_id']);self.assertTrue(q.ok);self.assertEqual(q.data['status'],'failed')
        self.transport.submit_state='processing';self.transfer(replace(self.ctx,idempotency_key='pending'))
        self.assertEqual(self.adapter.reconcile_statement(self.ctx)['status'],'PENDING_REVIEW')

    def test_post_dispatch_storage_loss_still_returns_stable_unknown_reference(self):
        original=self.transport.send
        def close_journal(operation,body,timeout):
            response=original(operation,body,timeout);self.adapter.db.close();return response
        with patch.object(self.transport,'send',side_effect=close_journal):result=self.transfer()
        self.assertTrue(result.ok);self.assertTrue(result.data['outcome_unknown']);self.assertTrue(result.data['transaction_id'].startswith('bankreq_'));self.assertEqual(self.transport.writes,1)
        self.adapter.close();self.adapter=self.make();self.assertTrue(self.transfer().data['idempotent_replay']);self.assertEqual(self.transport.writes,1)


class BankMainChainTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.transport=ProtocolFixture();self.binding=BankBinding('demo-user','demo-user','acct-demo','grant-demo',1,{'妈妈':'benef-mom'},('card_001',))
        self.service=self.new_service();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'runtime.sqlite3',offline=True,bank_service=self.service)
    def new_service(self):return BankServiceAdapter(Path(self.tmp.name)/'bank.sqlite3',self.transport,lambda actor:self.binding if actor=='demo-user' else None,lambda *args:None)
    def close_runtime(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
    def tearDown(self):self.close_runtime();self.tmp.cleanup()
    def transfer_plan(self):
        r=self.rt.chat('给妈妈转1元');self.assertTrue(r['ok'],r);return r
    def confirm(self,r):return self.rt.confirm(r['plan']['plan_id'],r['plan_view']['action_token'])
    def complete_card(self,r):
        challenge=self.confirm(r);self.assertTrue(challenge.get('needs_auth'),challenge)
        credential=challenge['auth_challenge']['credential_id'];verified=self.rt.verify(r['plan']['plan_id'],r['plan_view']['action_token'],'123456',credential_id=credential);self.assertTrue(verified['ok'],verified)
        return self.rt.confirm(r['plan']['plan_id'],r['plan_view']['action_token'],credential_id=credential)
    def test_existing_confirmation_uses_service_once_and_keeps_mock_ledger_unchanged(self):
        initial_mock=self.rt.bank._base.balance_cents;r=self.transfer_plan();self.assertEqual(self.transport.writes,0)
        done=self.confirm(r);self.assertEqual(done['result']['status'],'SUCCESS',done);self.assertEqual(self.transport.writes,1)
        self.assertIn('本地查询引用',done['result_view']['user_message']);self.assertIn('不是正式银行凭证',done['result_view']['user_message'])
        self.assertEqual(self.transport.balance,9999900);self.assertEqual(self.rt.bank._base.balance_cents,initial_mock)
        self.assertFalse(self.confirm(r)['ok']);self.assertEqual(self.transport.writes,1)
        self.assertTrue(self.rt.audit.verify_trace(r['trace_id'])['valid'])
        events=self.rt.audit.list_for_trace(r['trace_id']);self.assertIn('bank.receipt.verified',[e['event_type'] for e in events])
    def test_service_unavailable_before_confirmation_never_falls_back_to_mock(self):
        r=self.transfer_plan()
        with patch.object(self.transport,'send',side_effect=TimeoutError('private upstream details')):
            done=self.confirm(r);self.assertFalse(done['ok']);self.assertNotIn('private upstream',json.dumps(done))
        self.assertEqual(self.transport.writes,0)
    def test_unknown_transfer_recovers_after_restart_without_resubmission(self):
        r=self.transfer_plan();self.transport.fault='lost_response';done=self.confirm(r);self.assertEqual(done['result']['status'],'UNKNOWN',done)
        self.assertEqual(self.transport.writes,1);self.close_runtime();self.transport.fault=None
        self.service=self.new_service();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'runtime.sqlite3',offline=True,bank_service=self.service)
        status=self.rt.task_status(r['plan']['plan_id'],r['plan_view']['action_token'],'demo-user','demo-device')
        self.assertTrue(status['ok'],status);self.assertEqual(self.transport.writes,1)
        dag=self.rt.dag_store.get('plan-'+r['plan']['plan_id']);self.assertEqual(next(n for n in dag.nodes if n.tool=='create_transfer').status,'succeeded')
    def test_card_requires_original_strong_authentication_and_verifies_service_state(self):
        r=self.rt.chat('我的卡丢了帮我挂失');self.assertTrue(r['ok'],r);self.assertEqual(self.transport.writes,0)
        done=self.complete_card(r);self.assertEqual(done['result']['status'],'SUCCESS',done);self.assertEqual(self.transport.card_status,'reported_loss');self.assertEqual(self.transport.writes,1)
        self.assertIn('本地卡管协议演示',done['message'])
        self.assertEqual(self.rt.bank._base.cards[0]['status'],'正常')
    def test_unknown_card_does_not_run_verification_or_claim_completed(self):
        r=self.rt.chat('我的卡丢了帮我挂失');self.transport.fault='lost_response';before=len([c for c in self.transport.calls if c[0]=='query_card'])
        done=self.complete_card(r);self.assertEqual(done['result']['status'],'UNKNOWN',done)
        submitted=next(i for i,c in enumerate(self.transport.calls) if c[0]=='report_loss')
        self.assertFalse(any(c[0]=='query_card' for c in self.transport.calls[submitted+1:]))
        events=self.rt.audit.list_for_trace(r['trace_id']);self.assertNotIn('card.loss.completed',[e['event_type'] for e in events])
        self.transport.fault=None;self.rt.advance_dags();self.assertEqual(self.transport.writes,1)

    def test_processing_card_has_pending_user_receipt_and_preserves_no_retry(self):
        self.transport.submit_state='accepted';r=self.rt.chat('我的卡丢了帮我挂失');done=self.complete_card(r)
        self.assertEqual(done['result']['status'],'PROCESSING',done);self.assertEqual(done['result_view']['status'],'PROCESSING',done)
        self.assertNotIn('已挂失',done['message']);self.assertEqual(self.transport.writes,1)
    def test_live_production_or_unconfigured_mode_is_refused_and_family_cannot_expand_scope(self):
        for env in [{'APP_ENV':'production'},{'BANK_SERVICE_MODE':'live'}]:
            with patch.dict(os.environ,env):
                with self.assertRaises(ValueError):ba.AgentRuntime(db_path=Path(self.tmp.name)/'must-not-create.sqlite3',offline=True,bank_service=self.service)
        with patch.dict(os.environ,{'BANK_SERVICE_MODE':'protocol-test'}):
            with self.assertRaises(ValueError):ba.AgentRuntime(db_path=Path(self.tmp.name)/'must-not-create.sqlite3',offline=True)
        self.assertFalse((Path(self.tmp.name)/'must-not-create.sqlite3').exists())
        self.assertFalse(self.rt.chat('给妈妈转1元',actor_id='other')['ok']);self.assertEqual(self.transport.writes,0)


class BankProtocolBrowserTests(unittest.TestCase):
    def test_real_http_gateway_and_browser_confirmation_reconciliation(self):
        import subprocess
        from http.server import BaseHTTPRequestHandler,ThreadingHTTPServer
        from urllib.request import Request,build_opener,ProxyHandler
        node=os.getenv('C7_NODE_BINARY');module=os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:self.skipTest('C7 browser tooling not configured')
        fixture=ProtocolFixture()
        class GatewayHandler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                raw=self.rfile.read(int(self.headers['Content-Length']));body=json.loads(raw)
                reply=fixture.send(self.path.strip('/'),body,5);data=json.dumps(reply).encode()
                self.send_response(200);self.send_header('Content-Length',str(len(data)));self.end_headers();self.wfile.write(data)
        gateway=ThreadingHTTPServer(('127.0.0.1',0),GatewayHandler);gt=threading.Thread(target=gateway.serve_forever,daemon=True);gt.start()
        class LocalTransport:
            scope='LOCAL_PROTOCOL_TEST'
            def send(self,operation,body,timeout):
                request=Request('http://127.0.0.1:'+str(gateway.server_port)+'/'+operation,data=json.dumps(body).encode(),headers={'Content-Type':'application/json'})
                with build_opener(ProxyHandler({})).open(request,timeout=timeout) as response:return json.loads(response.read(32769))
        try:
            with tempfile.TemporaryDirectory() as tmp:
                binding=BankBinding('demo-user','demo-user','acct-demo','grant-demo',1,{'妈妈':'benef-mom'},('card_001',))
                service=BankServiceAdapter(Path(tmp)/'bank.sqlite3',LocalTransport(),lambda actor:binding if actor=='demo-user' else None,lambda *args:None)
                rt=ba.AgentRuntime(db_path=Path(tmp)/'app.sqlite3',offline=True,bank_service=service)
                class Handler(ba.Handler):
                    def log_message(self,*args):pass
                server=ba.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True)
                script=r'''const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true});try{const p=await browser.newPage();const errors=[];p.on('pageerror',e=>errors.push(e.message));await p.goto(process.env.BANK_PROTOCOL_URL);
await p.locator('#messageInput').fill('给妈妈转1元');await p.locator('#sendBtn').click();await p.locator('#confirmBtn').waitFor();
assert.equal(await p.evaluate(()=>currentResultView?.status==='SUCCESS'),false);await p.locator('#confirmBtn').click();
try{await p.waitForFunction(()=>currentResultView?.status==='SUCCESS')}catch(e){throw new Error(JSON.stringify({errors,view:await p.evaluate(()=>currentResultView),body:(await p.locator('body').innerText()).slice(-2000)}))}
await p.waitForFunction(()=>!busy);assert.ok((await p.locator('#balanceValue').innerText()).includes('99,999.00'));assert.ok((await p.locator('#chatLog').innerText()).includes('本地查询引用'));
const data=await p.evaluate(async()=>{const h={Authorization:'Bearer demo-user-session'};return {cap:await (await fetch('/api/v1/bank/capabilities',{headers:h})).json(),statement:await(await fetch('/api/v1/bank/reconciliation',{headers:h})).json()}});
assert.equal(data.cap.mode,'LOCAL_PROTOCOL_TEST');assert.equal(data.cap.live_bank_connected,false);assert.equal(data.statement.status,'MATCHED');assert.deepEqual(errors,[]);
if(process.env.BANK_PROTOCOL_SCREENSHOT_DIR)await p.screenshot({path:process.env.BANK_PROTOCOL_SCREENSHOT_DIR+'/bank-protocol.png',fullPage:true});console.log('BANK_PROTOCOL_BROWSER_PASS');
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});'''
                try:
                    with patch.object(ba,'runtime',rt),patch.object(ba,'gateway',ba.AgentGateway(rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True):
                        thread.start();env=dict(os.environ,BANK_PROTOCOL_URL=f'http://127.0.0.1:{server.server_port}')
                        result=subprocess.run([node,'-e',script],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=70)
                        self.assertEqual(result.returncode,0,result.stdout.decode('utf-8',errors='replace'));self.assertEqual(fixture.writes,1)
                finally:server.shutdown();server.server_close();thread.join(timeout=5);rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
        finally:gateway.shutdown();gateway.server_close();gt.join(timeout=5)


if __name__=='__main__':unittest.main(verbosity=2)
