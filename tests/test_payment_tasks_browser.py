import os,subprocess,threading,tempfile,json,urllib.request,urllib.error,unittest
from pathlib import Path
from unittest.mock import patch
import banking_agent as ba

class PaymentTaskHttpBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'resume.sqlite3',offline=True)
    def tearDown(self):self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
    def serve(self,callback):
        with patch.object(ba,'runtime',self.rt),patch.object(ba,'gateway',ba.AgentGateway(self.rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True),ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:callback(f'http://127.0.0.1:{server.server_port}')
            finally:server.shutdown();thread.join(timeout=5)
    def test_resume_http_rejects_missing_session_spoof_and_unsafe_fields(self):
        old=self.rt.chat('给妈妈转1元');task=old['business_task'];tid=task['task_id']
        def check(base):
            for token,extra,status in [('',{},403),('demo-user-session',{'device_id':'forged'},403),('demo-user-session',{'approved':True},400),('demo-user-session',{},200)]:
                data={'task_id':tid,'expected_version':task['version'],'changes':{},**extra}
                request=urllib.request.Request(base+'/api/v1/business-tasks/resume',data=json.dumps(data).encode(),headers={'Content-Type':'application/json',**({'Authorization':'Bearer '+token} if token else {})})
                try:
                    with urllib.request.urlopen(request,timeout=10) as response:actual=response.status;body=json.load(response)
                except urllib.error.HTTPError as error:actual=error.code;body=json.loads(error.read());error.close()
                self.assertEqual(actual,status,body)
                if actual==200:self.assertTrue(body['reprepared']);self.assertFalse(body['business_executed'])
        self.serve(check);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_browser_reload_edit_reprepare_then_native_confirm_once(self):
        node,module=os.getenv('C7_NODE_BINARY'),os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:self.skipTest('C7_NODE_BINARY/C7_PLAYWRIGHT_MODULE required')
        self.rt.bank.confirmed_commitments=[{'id':'rent','name':'房租','amount_cents':40000000,'status':'confirmed'}]
        old=self.rt.chat('给妈妈转1元');self.rt.bank.confirmed_commitments=[];task=old['business_task']
        script=r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{const browser=await chromium.launch({headless:true});try{
const page=await browser.newPage({viewport:{width:1280,height:1050}});await page.goto(process.env.TASK_RESUME_URL);await page.reload();
await page.locator('[data-business-task="'+process.env.TASK_RESUME_ID+'"]').click();await page.locator('#taskResumeAmount').fill('2.50');
if(process.env.TASK_RESUME_SCREENSHOT)await page.screenshot({path:process.env.TASK_RESUME_SCREENSHOT,fullPage:true});
const [response]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/v1/business-tasks/resume')&&r.request().method()==='POST'),page.locator('#taskResumeBtn').click()]);const fresh=await response.json();
if(!fresh.reprepared||fresh.business_executed!==false||fresh.plan.entities.amount_cents!==250)throw Error('Incorrect resume');
await page.locator('#paymentEffectScope').waitFor();await page.locator('#confirmBtn').waitFor();
if(process.env.TASK_RESUME_SCREENSHOT){await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:process.env.TASK_RESUME_SCREENSHOT,fullPage:true});}
const done=page.waitForResponse(r=>r.url().endsWith('/api/confirm')&&r.request().method()==='POST');await page.locator('#confirmBtn').click();const body=await(await done).json();if(body.result.status!=='SUCCESS')throw Error('Native confirmation failed');
await page.locator('#refreshBusinessTasks').click();await page.locator('[data-business-task="'+process.env.TASK_RESUME_ID+'"]').click();await page.locator('#businessTaskDetail').getByText('当前状态：已核实完成',{exact:true}).waitFor();
if(await page.locator('#taskResumeBtn').count())throw Error('Completed task still offers reprepare');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
"""
        def check(base):
            p=subprocess.run([node,'-e',script],env=dict(os.environ,TASK_RESUME_URL=base,TASK_RESUME_ID=task['task_id']),capture_output=True,text=True,encoding='utf-8',timeout=65)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.serve(check);self.assertEqual(self.rt.bank.daily_transfer_cents,250)

if __name__=='__main__':unittest.main()
