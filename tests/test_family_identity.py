import tempfile,unittest,os,json
from pathlib import Path
from unittest.mock import patch
from datetime import datetime,timezone
import banking_agent as ba
from identity_service import totp
class FamilyIdentityTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory()
  with patch.dict(os.environ,{'IDENTITY_MODE':'self-hosted'}):self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'family.sqlite3',offline=True)
  self.s=self.rt.identity_service;self.now=[1800000000.];self.s.clock=lambda:self.now[0];self.rt.bank.guardianships.clock=lambda:datetime.fromtimestamp(self.now[0],timezone.utc)
  self.seed='JBSWY3DPEHPK3PXP';self.password='Synthetic Family password 47!';self.f='family-father-demo';self.d='family-daughter-demo';self.g='grant-daughter-father'
  self.sessions={}
  for user in [self.f,self.d]:self.s.provision(user,self.password,secret=self.seed);self.sessions[user]=self.s.login(user,self.password,totp(self.seed,self.now[0]))
 def tearDown(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
 def session(self,user):item=self.sessions[user];return self.s.session(item['token'],item['device'])
 def permissions(self,read=True,notify=False,assist=False):
  self.now[0]+=30
  with self.session(self.f):return self.rt.family.set_permissions(self.g,self.rt.bank.guardianships.consent(self.g)['version'],{'read':read,'notify':notify,'assist':assist},totp(self.seed,self.now[0]))
 def draft(self):
  with self.session(self.d):return self.rt.family.chat('给妈妈转1元',self.d,self.s.current_identity()['device_id'],self.f)
 def test_shipped_relationships_do_not_enable_selfhosted_read(self):
  with self.session(self.d):
   out=self.rt.family.chat('查余额',self.d,self.s.current_identity()['device_id'],self.f);self.assertFalse(out['ok'])
  self.assertEqual(self.rt.bank.guardianships.guardian_targets(self.f),[])
 def test_owner_approves_read_but_guardian_cannot_approve(self):
  with self.session(self.d):self.assertFalse(self.rt.family.set_permissions(self.g,0,{'read':True,'notify':True,'assist':True},'123456')['ok'])
  self.assertTrue(self.permissions()['ok'])
  with self.session(self.d):
   out=self.rt.family.chat('查余额',self.d,self.s.current_identity()['device_id'],self.f);self.assertTrue(out['ok'],out)
  self.assertFalse(self.draft()['ok'])
 def test_dual_identity_owner_totp_and_exactly_once_transfer(self):
  self.assertTrue(self.permissions(assist=True)['ok']);out=self.draft();self.assertTrue(out['ok'],out);pid=out['plan']['plan_id'];token=out['plan']['consent_token']
  with self.session(self.d):self.assertFalse(self.rt.confirm(pid,token,actor_id=self.d,device_id=self.s.current_identity()['device_id'])['ok'])
  with self.session(self.f):
   reviewed=self.rt.family.review(pid);self.assertTrue(reviewed['ok']);challenge=self.rt.confirm(pid,token,actor_id=self.f,device_id=self.s.current_identity()['device_id']);self.assertTrue(challenge['needs_auth']);cid=challenge['auth_challenge']['credential_id'];self.now[0]+=30
   self.assertTrue(self.rt.verify(pid,token,totp(self.seed,self.now[0]),credential_id=cid,actor_id=self.f,device_id=self.s.current_identity()['device_id'])['ok'])
   done=self.rt.confirm(pid,token,credential_id=cid,actor_id=self.f,device_id=self.s.current_identity()['device_id']);self.assertEqual(done['result']['status'],'SUCCESS');self.assertFalse(self.rt.confirm(pid,token)['ok'])
  self.assertEqual(self.rt.bank.available_balance(self.f),1999900)
 def test_revocation_blocks_existing_draft_before_side_effect(self):
  self.permissions(assist=True);out=self.draft();pid=out['plan']['plan_id'];self.permissions(False,False,False)
  with self.session(self.f):
   denied=self.rt.confirm(pid,out['plan_view']['action_token'],actor_id=self.f,device_id=self.s.current_identity()['device_id']);self.assertFalse(denied['ok'])
  self.assertEqual(self.rt.bank.available_balance(self.f),2000000)
 def test_notification_permission_is_separate_and_minimal(self):
  self.permissions(False,True,False);self.assertEqual(self.rt.bank.guardianships.guardian_targets(self.f),[self.d])
  with self.session(self.d):self.assertFalse(self.rt.family.chat('查余额',self.d,self.s.current_identity()['device_id'],self.f)['ok'])
 def test_stale_revision_wrong_otp_and_spoofed_device_rejected(self):
  self.permissions()
  with self.session(self.f):self.assertFalse(self.rt.family.set_permissions(self.g,0,{'read':True,'notify':False,'assist':False},totp(self.seed,self.now[0]))['ok'])
  with self.session(self.d):self.assertFalse(self.rt.family.chat('查余额',self.d,'daughter-device',self.f)['ok'])
 def test_browser_two_identities_permission_draft_and_owner_review(self):
  import subprocess,threading
  node,module=os.getenv('C7_NODE_BINARY'),os.getenv('C7_PLAYWRIGHT_MODULE')
  if not node or not module:self.skipTest('Browser runtime required')
  self.now[0]+=30
  script=r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{const b=await chromium.launch({headless:true});try{
const base=process.env.FAMILY_URL,items=JSON.parse(process.env.FAMILY_SESSIONS),contexts={};
for(const key of Object.keys(items)){const c=await b.newContext();await c.addCookies([{name:'bank_session',value:items[key].token,url:base},{name:'bank_device',value:items[key].device,url:base}]);contexts[key]=await c.newPage();}
const owner=contexts['family-father-demo'],guardian=contexts['family-daughter-demo'];await owner.goto(base);
const box=owner.locator('[data-family-grant="grant-daughter-father"]');await box.locator('[data-family-scope="read"]').check();await box.locator('[data-family-scope="assist"]').check();await box.getByLabel('核准家庭授权的动态码').fill(process.env.FAMILY_CODE);
const [saved]=await Promise.all([owner.waitForResponse(r=>r.url().endsWith('/api/v1/family/permissions')&&r.request().method()==='POST'),box.locator('[data-family-save]').click()]);if(!(await saved.json()).ok)throw Error('Owner consent failed');
await guardian.goto(base);const g=guardian.locator('[data-family-grant="grant-daughter-father"]');await g.getByLabel('家庭协助需求').fill('给妈妈转1元');const [drafted]=await Promise.all([guardian.waitForResponse(r=>r.url().endsWith('/api/v1/family/chat')),g.locator('[data-family-draft]').click()]);const draft=await drafted.json();if(!draft.ok||draft.plan_view.requires_confirmation)throw Error('Guardian draft or owner-only confirmation failed');
await owner.locator('#familyReviewTask').fill(draft.plan.plan_id);const [reviewed]=await Promise.all([owner.waitForResponse(r=>r.url().endsWith('/api/v1/family/review')),owner.locator('#familyReviewBtn').click()]);if(!(await reviewed.json()).ok)throw Error('Owner review failed');await owner.locator('#confirmBtn').waitFor();
if(process.env.FAMILY_SCREENSHOT){await owner.evaluate(()=>window.scrollTo(0,0));await owner.screenshot({path:process.env.FAMILY_SCREENSHOT,fullPage:true});}
}finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1});
"""
  with patch.object(ba,'runtime',self.rt),patch.object(ba,'gateway',ba.AgentGateway(self.rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True),ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   try:
    result=subprocess.run([node,'-e',script],env=dict(os.environ,FAMILY_URL=f'http://127.0.0.1:{server.server_port}',FAMILY_SESSIONS=json.dumps(self.sessions),FAMILY_CODE=totp(self.seed,self.now[0])),capture_output=True,text=True,encoding='utf-8',timeout=65)
    self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertEqual(self.rt.bank.available_balance(self.f),2000000)
   finally:server.shutdown();thread.join(timeout=5)
 def test_owner_dynamic_code_replay_cannot_change_scope(self):
  self.assertTrue(self.permissions()['ok'])
  with self.session(self.f):
   prior=self.rt.bank.guardianships.consent(self.g)
   out=self.rt.family.set_permissions(self.g,prior['version'],{'read':False,'notify':False,'assist':False},totp(self.seed,self.now[0]));self.assertFalse(out['ok']);self.assertEqual(self.rt.bank.guardianships.consent(self.g),prior)

 def test_family_identity_cannot_call_global_reset_or_security_runner(self):
  import urllib.request,urllib.error,threading
  item=self.sessions[self.d]
  with patch.object(ba,'runtime',self.rt),patch.object(ba,'gateway',ba.AgentGateway(self.rt)),ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
   thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
   try:
    base=f'http://127.0.0.1:{server.server_port}'
    for path in ['/api/reset-demo','/api/v1/security/attack-matrix/run']:
     request=urllib.request.Request(base+path,data=b'{}',headers={'Content-Type':'application/json','Origin':base,'Cookie':'bank_session='+item['token']+'; bank_device='+item['device']})
     try:
      with urllib.request.urlopen(request,timeout=10) as response:status=response.status
     except urllib.error.HTTPError as error:status=error.code;error.read();error.close()
     self.assertEqual(status,403,path)
   finally:server.shutdown();thread.join(timeout=5)

if __name__=='__main__':unittest.main()
