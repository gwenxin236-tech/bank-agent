"""P09 local identity is real password/TOTP verification, never bank certification."""
import hashlib,json,os,sys,tempfile,threading,unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT/'app'))
import banking_agent as ba
from identity_service import IdentityService,totp


class IdentityTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'identity.sqlite3'
        with patch.dict(os.environ,{'IDENTITY_MODE':'self-hosted'}):self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
        self.s=self.rt.identity_service;self.now=[1800000000.];self.s.clock=lambda:self.now[0]
        self.secret='JBSWY3DPEHPK3PXP';self.password='Local test password 927!'
        self.s.provision('demo-user',self.password,'user',self.secret)
    def tearDown(self):
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
    def login(self):return self.s.login('demo-user',self.password,totp(self.secret,self.now[0]))
    def tick(self):self.now[0]+=30
    def test_rfc6238_vectors(self):
        seed='GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ'
        for instant,want in [(59,'94287082'),(1111111109,'07081804'),(1111111111,'14050471'),(1234567890,'89005924'),(2000000000,'69279037'),(20000000000,'65353130')]:
            self.assertEqual(totp(seed,instant,digits=8),want)
    def test_password_totp_and_server_roles_not_client_assertions(self):
        self.assertIsNone(self.s.login('demo-user','wrong',totp(self.secret,self.now[0])))
        self.assertIsNone(self.s.login('demo-user',self.password,'000000'))
        session=self.login();self.assertIsNotNone(session)
        with self.s.session(session['token'],session['device']):
            context=self.s.current_identity();self.assertEqual(context['role'],'user')
            self.assertNotEqual(context['device_id'],'demo-device')
        serialized=' '.join(str(tuple(r)) for r in self.s.db.execute('SELECT * FROM identity_accounts'))
        self.assertNotIn(self.password,serialized)
    def test_login_otp_replay_and_wrong_device_rejected(self):
        item=self.login();self.assertIsNone(self.login())
        with self.assertRaises(PermissionError):
            with self.s.session(item['token'],'different-device'):pass
    def test_expiry_logout_and_account_revocation(self):
        item=self.login();self.now[0]+=self.s.IDLE_TTL+1
        with self.assertRaises(PermissionError):
            with self.s.session(item['token'],item['device']):pass
        self.tick();item=self.login();self.s.logout(item['token'])
        with self.assertRaises(PermissionError):
            with self.s.session(item['token'],item['device']):pass
        self.tick();item=self.login();self.s.revoke('demo-user')
        with self.assertRaises(PermissionError):
            with self.s.session(item['token'],item['device']):pass
    def test_account_failure_limit_persists(self):
        for _ in range(5):self.assertIsNone(self.s.login('demo-user','wrong','000000'))
        self.assertIsNone(self.login());self.now[0]+=self.s.LOCK_SECONDS+30
        self.assertIsNotNone(self.login())
    def test_legacy_bearer_and_client_identity_spoof_rejected(self):
        out=self.rt.family.http_context('Bearer demo-user-session',{},required=True);self.assertFalse(out['ok'])
        item=self.login()
        with self.s.session(item['token'],item['device']):
            self.assertFalse(self.rt.family.http_context('',{'user_id':'other'},required=True)['ok'])
            self.assertFalse(self.rt.family.http_context('',{'device_id':'demo-device'},required=True)['ok'])
    def challenge(self,item):
        identity=self.s.current_identity();out=self.rt.chat('给妈妈转2000元',actor_id='demo-user',device_id=identity['device_id'])
        pid=out['plan']['plan_id'];token=out['plan_view']['action_token'];challenge=self.rt.confirm(pid,token)
        self.assertTrue(challenge['needs_auth']);self.assertEqual(challenge['auth_challenge']['method'],'totp')
        return pid,token,challenge['auth_challenge']['credential_id']
    def test_original_chain_totp_no_fixed_otp_no_repeat_debit(self):
        item=self.login();before=self.rt.bank.balance_cents
        with self.s.session(item['token'],item['device']):
            pid,token,cid=self.challenge(item);self.assertEqual(self.rt.bank.balance_cents,before)
            self.assertFalse(self.rt.verify(pid,token,'123456',credential_id=cid)['ok']);self.tick()
            self.assertTrue(self.rt.verify(pid,token,totp(self.secret,self.now[0]),credential_id=cid)['ok'])
            out=self.rt.confirm(pid,token,credential_id=cid);self.assertEqual(out['result']['status'],'SUCCESS')
            self.assertFalse(self.rt.confirm(pid,token,credential_id=cid)['ok']);self.assertEqual(self.rt.bank.balance_cents,before-200000)
    def test_changed_operation_and_foreign_session_cannot_use_challenge(self):
        item=self.login()
        with self.s.session(item['token'],item['device']):pid,token,cid=self.challenge(item)
        self.tick();second=self.login()
        with self.s.session(second['token'],second['device']):
            self.assertFalse(self.rt.confirm(pid,token,credential_id=cid)['ok'])
        with self.s.session(item['token'],item['device']):
            self.rt.pending[pid].entities['amount_cents']=190000
            self.assertFalse(self.rt.verify(pid,token,totp(self.secret,self.now[0]),credential_id=cid)['ok'])
        self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_concurrent_factor_verification_once_and_durable_session(self):
        item=self.login();self.tick()
        with self.s.session(item['token'],item['device']):pid,token,cid=self.challenge(item)
        plan=self.rt.pending[pid]
        def verify(_):
            with self.s.session(item['token'],item['device']):
                ctx=self.rt._identity_context(plan)
                return self.s.verify(ctx,cid,totp(self.secret,self.now[0]),self.rt._auth_digest(plan)).ok
        with ThreadPoolExecutor(max_workers=4) as pool:self.assertEqual(sum(pool.map(verify,range(4))),1)
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
        with patch.dict(os.environ,{'IDENTITY_MODE':'self-hosted'}):self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
        self.s=self.rt.identity_service;self.s.clock=lambda:self.now[0]
        with self.s.session(item['token'],item['device']):self.assertEqual(self.s.current_identity()['user_id'],'demo-user')
    def test_identity_device_protocols_and_wrong_subject(self):
        from production_contracts import AdapterContext
        item=self.login()
        with self.s.session(item['token'],item['device']):
            c=self.s.current_identity();ctx=AdapterContext('trace-idp','demo-user',c['device_id'],subject_id='demo-user',authorization_ref=c['session_id'])
            self.assertTrue(self.s.verify_subject(ctx,item['token']).ok);self.assertTrue(self.s.assess(ctx,item['device']).ok)
            self.assertFalse(self.s.verify_subject(replace(ctx,subject_id='someone-else'),item['token']).ok)
            self.assertFalse(self.s.assess(ctx,'forged-device').ok)
    def test_session_entry_uses_identity_and_device_providers(self):
        from production_contracts import AdapterContext
        item=self.login();ctx=AdapterContext('trace-denied','demo-user')
        for method in ['verify_subject','assess']:
            with patch.object(self.s,method,return_value=self.s.result(ctx,False,'PROVIDER_DENIED')):
                with self.assertRaises(PermissionError):
                    with self.s.session(item['token'],item['device']):pass
    def test_verified_device_maps_to_local_bank_only_inside_authenticated_context(self):
        from bank_service import BankServiceAdapter,BankBinding
        from test_bank_service import ProtocolFixture
        fixture=ProtocolFixture()
        class Transport:
            scope='LOCAL_PROTOCOL_TEST'
            def send(self,operation,body,timeout):return fixture.send(operation,body,timeout)
        binding=BankBinding('demo-user','demo-user','acct-demo','grant-demo',1,{'妈妈':'benef-mom'},('card_001',))
        adapter=BankServiceAdapter(Path(self.tmp.name)/'bank-adapter.sqlite3',Transport(),lambda actor:binding if actor=='demo-user' else None,lambda *args:None)
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
        with patch.dict(os.environ,{'IDENTITY_MODE':'self-hosted'}):self.rt=ba.AgentRuntime(db_path=self.path,offline=True,bank_service=adapter)
        self.s=self.rt.identity_service;self.s.clock=lambda:self.now[0];item=self.login()
        with self.s.session(item['token'],item['device']):
            who=self.s.current_identity();ctx=adapter.context('demo-user',device_id=who['device_id']);self.assertEqual(adapter._binding(ctx).subject_id,'demo-user')
            out=self.rt.chat('给妈妈转2000元',device_id=who['device_id']);self.assertIn('plan',out)
            with self.assertRaises(ValueError):adapter._binding(replace(ctx,device_id='forged-device'))
        with self.assertRaises(ValueError):adapter._binding(ctx)
    def test_audit_failure_rolls_back_session_creation(self):
        with patch.object(self.rt.audit,'write',side_effect=OSError('audit unavailable')):
            with self.assertRaises(OSError):self.login()
        self.assertEqual(self.s.db.execute('SELECT count(*) FROM identity_sessions').fetchone()[0],0)
    def test_no_credentials_codes_or_session_tokens_in_audit(self):
        item=self.login()
        raw=' '.join(r[0] for r in self.s.db.execute('SELECT payload_json FROM audit_events'))
        for value in [self.password,self.secret,item['token'],item['device'],totp(self.secret,self.now[0])]:self.assertNotIn(value,raw)

    def test_direct_runtime_without_verified_session_cannot_submit(self):
        self.assertFalse(self.rt.chat('给妈妈转1元')['ok']);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_rotation_invalidates_existing_sessions_and_preserves_server_role(self):
        item=self.login();new_secret='KRSXG5DSNFXGOIDB';new_password='Rotated local password 823!'
        self.s.rotate('demo-user',new_password,new_secret)
        with self.assertRaises(PermissionError):
            with self.s.session(item['token'],item['device']):pass
        self.assertIsNone(self.login());fresh=self.s.login('demo-user',new_password,totp(new_secret,self.now[0]))
        with self.s.session(fresh['token'],fresh['device']):self.assertEqual(self.s.current_identity()['role'],'user')
    def test_absolute_expiry_and_factor_consumption_reject_revoked_account(self):
        item=self.login()
        with self.s.session(item['token'],item['device']):
            pid,token,cid=self.challenge(item);plan=self.rt.pending[pid];ctx=self.rt._identity_context(plan);self.tick()
            self.assertTrue(self.s.verify(ctx,cid,totp(self.secret,self.now[0]),self.rt._auth_digest(plan)).ok)
            self.s.revoke('demo-user');self.assertFalse(self.s.consume(ctx,cid,self.rt._auth_digest(plan)).ok)
        self.s.rotate('demo-user',self.password,self.secret);self.tick();item=self.login();self.now[0]+=self.s.SESSION_TTL
        with self.assertRaises(PermissionError):
            with self.s.session(item['token'],item['device']):pass
    def test_session_and_bound_challenge_survive_runtime_restart_without_second_debit(self):
        item=self.login()
        with self.s.session(item['token'],item['device']):
            pid,token,cid=self.challenge(item);self.tick()
            self.assertTrue(self.rt.verify(pid,token,totp(self.secret,self.now[0]),credential_id=cid)['ok'])
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close()
        with patch.dict(os.environ,{'IDENTITY_MODE':'self-hosted'}):self.rt=ba.AgentRuntime(db_path=self.path,offline=True)
        self.s=self.rt.identity_service;self.s.clock=lambda:self.now[0]
        with self.s.session(item['token'],item['device']):
            self.assertEqual(self.rt.confirm(pid,token,credential_id=cid)['result']['status'],'SUCCESS')
            self.assertFalse(self.rt.confirm(pid,token,credential_id=cid)['ok'])
        self.assertEqual(self.rt.bank.daily_transfer_cents,200000)
    def test_factor_failures_are_limited_across_different_challenges(self):
        item=self.login()
        with self.s.session(item['token'],item['device']):
            for _ in range(5):
                pid,token,cid=self.challenge(item);plan=self.rt.pending[pid]
                self.assertFalse(self.s.verify(self.rt._identity_context(plan),cid,'000000',self.rt._auth_digest(plan)).ok)
            self.tick();pid,token,cid=self.challenge(item);plan=self.rt.pending[pid]
            self.assertEqual(self.s.verify(self.rt._identity_context(plan),cid,totp(self.secret,self.now[0]),self.rt._auth_digest(plan)).code,'AUTH_LOCKED')
    def test_provider_expiry_attempt_limit_and_durable_single_consumption(self):
        item=self.login()
        with self.s.session(item['token'],item['device']):
            pid,token,cid=self.challenge(item);plan=self.rt.pending[pid];ctx=self.rt._identity_context(plan)
            for _ in range(3):self.assertFalse(self.s.verify(ctx,cid,'000000',self.rt._auth_digest(plan)).ok)
            self.tick();self.assertEqual(self.s.verify(ctx,cid,totp(self.secret,self.now[0]),self.rt._auth_digest(plan)).code,'AUTH_LOCKED')
            pid,token,cid=self.challenge(item);plan=self.rt.pending[pid];ctx=self.rt._identity_context(plan)
            self.now[0]+=301;self.assertEqual(self.s.verify(ctx,cid,totp(self.secret,self.now[0]),self.rt._auth_digest(plan)).code,'AUTH_EXPIRED')
            pid,token,cid=self.challenge(item);plan=self.rt.pending[pid];ctx=self.rt._identity_context(plan)
            self.assertTrue(self.s.verify(ctx,cid,totp(self.secret,self.now[0]),self.rt._auth_digest(plan)).ok)
            self.assertTrue(self.s.consume(ctx,cid,self.rt._auth_digest(plan)).ok)
            self.assertEqual(self.s.consume(ctx,cid,self.rt._auth_digest(plan)).code,'AUTH_REPLAY')
    def test_http_login_origin_role_cookie_flags_logout_and_no_demo_bypass(self):
        import http.client
        class Handler(ba.Handler):
            def log_message(self,*args):pass
        server=ba.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True)
        base='http://127.0.0.1:'+str(server.server_port)
        def request(path,body=None,cookie='',origin=None,extra=None):
            con=http.client.HTTPConnection('127.0.0.1',server.server_port,timeout=10)
            headers={'Content-Type':'application/json','Cookie':cookie,'Origin':origin or base,**(extra or {})}
            con.request('POST' if body is not None else 'GET',path,json.dumps(body).encode() if body is not None else None,headers)
            response=con.getresponse();result=(response.status,response.getheaders(),response.read());con.close();return result
        try:
            with patch.object(ba,'runtime',self.rt),patch.object(ba,'gateway',ba.AgentGateway(self.rt)):
                thread.start()
                self.assertEqual(request('/api/state',extra={'Authorization':'Bearer demo-user-session'})[0],403)
                body={'username':'demo-user','password':self.password,'code':totp(self.secret,self.now[0])}
                self.assertEqual(request('/api/v1/identity/login',body,origin='http://evil.invalid')[0],403)
                status,headers,raw=request('/api/v1/identity/login',body);self.assertEqual(status,200)
                cookies=[v for k,v in headers if k.lower()=='set-cookie'];self.assertEqual(len(cookies),2)
                self.assertTrue(all('HttpOnly' in v and 'SameSite=Strict' in v for v in cookies));cookie='; '.join(v.split(';')[0] for v in cookies)
                state=json.loads(request('/api/state',cookie=cookie)[2]);self.assertNotIn('demo_otp',state)
                self.assertEqual(request('/api/v1/customer-service',{'request_id':'owner','user_id':'forged'},cookie)[0],403)
                self.assertEqual(request('/api/v1/handoff/inbox',{},cookie)[0],403)
                self.assertEqual(request('/api/v1/identity/logout',{},cookie)[0],200);self.assertEqual(request('/api/state',cookie=cookie)[0],403)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)
    def test_browser_login_real_cookie_red_transfer_and_operator_isolation(self):
        import subprocess
        node=os.getenv('C7_NODE_BINARY');module=os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:self.skipTest('local C7 browser tooling required')
        self.s.provision('local-operator',self.password,'admin','KRSXG5DSNFXGOIDB')
        def browser_context():
            # Test-only controlled time, never an application HTTP endpoint.
            self.tick();return totp(self.secret,self.now[0])
        class Handler(ba.Handler):
            def log_message(self,*args):pass
            def do_POST(handler):
                if handler.path=='/qa-next-factor':return handler._send(200,{'code':browser_context()})
                return super().do_POST()
        server=ba.ThreadingHTTPServer(('127.0.0.1',0),Handler);thread=threading.Thread(target=server.serve_forever,daemon=True)
        script=r'''const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true});try{const uc=await browser.newContext(),oc=await browser.newContext();const u=await uc.newPage(),op=await oc.newPage();
await u.goto(process.env.IDENTITY_URL+'/');await u.waitForURL('**/identity/login');await u.locator('#identityUser').fill('demo-user');await u.locator('#identityPassword').fill(process.env.IDENTITY_PASSWORD);await u.locator('#identityCode').fill(process.env.IDENTITY_LOGIN_CODE);await u.locator('#identitySubmit').click();await u.locator('#identitySession').waitFor();
assert.equal(await u.evaluate(()=>document.cookie.includes('bank_session')),false);assert.equal(await u.evaluate(()=>Object.values(localStorage).some(v=>v.includes('bank_session'))),false);
await u.locator('#messageInput').fill('给妈妈转2000元');await u.locator('#sendBtn').click();await u.locator('#confirmBtn').waitFor();await u.locator('#confirmBtn').click();await u.locator('#authModal').waitFor({state:'visible'});assert.ok((await u.locator('#authDescription').textContent()).includes('身份验证器'));
const code=await u.evaluate(async()=> (await(await fetch('/qa-next-factor',{method:'POST'})).json()).code);await u.locator('#otpInput').fill(code);await u.locator('#verifyBtn').click();await u.locator('#authModal').waitFor({state:'hidden'});await u.waitForFunction(()=>!busy);await u.locator('#confirmBtn').click();await u.waitForFunction(()=>currentResultView?.status==='SUCCESS');
await u.locator('#customerServiceBtn').click();await u.locator('#liveSupportText').waitFor();await u.locator('#liveSupportText').fill('请核对，不办理业务');await u.locator('#liveSupportSend').click();
await op.goto(process.env.IDENTITY_URL+'/support/operator');await op.waitForURL('**/identity/login');await op.locator('#identityUser').fill('local-operator');await op.locator('#identityPassword').fill(process.env.IDENTITY_PASSWORD);await op.locator('#identityCode').fill(process.env.IDENTITY_OPERATOR_CODE);await op.locator('#identitySubmit').click();await op.waitForURL('**/support/operator');await op.waitForFunction(()=>document.querySelector('#operatorToken').disabled);await op.locator('#operatorLogin').click();await op.locator('#operatorCases button').first().click();await op.locator('#operatorMessages').getByText('请核对，不办理业务',{exact:true}).waitFor();
assert.equal(await op.evaluate(async()=> (await fetch('/api/state')).status),403);await u.locator('#identityLogout').click();await u.waitForURL('**/identity/login');assert.equal(await u.evaluate(async()=> (await fetch('/api/state')).status),403);
console.log('P09_BROWSER_PASS');}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});'''
        try:
            with patch.object(ba,'runtime',self.rt),patch.object(ba,'gateway',ba.AgentGateway(self.rt)):
                thread.start();env=dict(os.environ,IDENTITY_URL='http://127.0.0.1:'+str(server.server_port),IDENTITY_PASSWORD=self.password,IDENTITY_LOGIN_CODE=totp(self.secret,self.now[0]),IDENTITY_OPERATOR_CODE=totp('KRSXG5DSNFXGOIDB',self.now[0]+30))
                result=subprocess.run([node,'-e',script],env=env,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,timeout=90)
                self.assertEqual(result.returncode,0,result.stdout.decode('utf-8',errors='replace'));self.assertEqual(self.rt.bank.daily_transfer_cents,200000)
        finally:server.shutdown();server.server_close();thread.join(timeout=5)


if __name__=='__main__':unittest.main(verbosity=2)
