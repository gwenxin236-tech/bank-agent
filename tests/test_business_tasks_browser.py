import os,subprocess,threading,tempfile,json,urllib.request,urllib.error
from pathlib import Path
from unittest.mock import patch
import unittest
import banking_agent as ba

class BusinessTaskHttpBrowserTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.rt=ba.AgentRuntime(db_path=Path(self.tmp.name)/'tasks.sqlite3',offline=True)
    def tearDown(self):
        self.rt.recovery_scheduler.stop();self.rt.bank.close();self.rt.audit.db.close();self.tmp.cleanup()
    def serve(self,callback):
        with patch.object(ba,'runtime',self.rt),patch.object(ba,'gateway',ba.AgentGateway(self.rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True),ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            try:callback(f'http://127.0.0.1:{server.server_port}')
            finally:server.shutdown();thread.join(timeout=5)
    def test_http_requires_session_and_does_not_accept_body_identity(self):
        task=self.rt.chat('给妈妈转1元')['business_task']
        def check(base):
            for path,token,expected in [('/api/v1/business-tasks','',403),('/api/v1/business-tasks','demo-user-session',200),('/api/v1/business-tasks?user_id=other','demo-user-session',403),('/api/v1/business-tasks/missing','demo-user-session',404)]:
                request=urllib.request.Request(base+path,headers={'Authorization':'Bearer '+token} if token else {})
                try:
                    with urllib.request.urlopen(request,timeout=10) as response:status=response.status;body=json.load(response)
                except urllib.error.HTTPError as error:
                    status=error.code;body=json.loads(error.read());error.close()
                self.assertEqual(status,expected)
                if status==200:self.assertEqual(body['items'][0]['task_id'],task['task_id'])
        self.serve(check);self.assertEqual(self.rt.bank.daily_transfer_cents,0)
    def test_browser_reload_reads_durable_tasks_without_confirmation(self):
        node,module=os.getenv('C7_NODE_BINARY'),os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:self.skipTest('Browser QA requires C7_NODE_BINARY/C7_PLAYWRIGHT_MODULE')
        self.rt.bank.confirmed_commitments=[{'id':'rent','name':'房租','amount_cents':40000000,'status':'confirmed'}]
        task=self.rt.chat('给妈妈转1元')['business_task']
        script=r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{const browser=await chromium.launch({headless:true});try{
const page=await browser.newPage({viewport:{width:1280,height:1000}});await page.goto(process.env.BANK_TASK_URL);
await page.locator('[data-business-task="'+process.env.BANK_TASK_ID+'"]').waitFor();
await page.reload();await page.locator('[data-business-task="'+process.env.BANK_TASK_ID+'"]').click();
await page.locator('#businessTaskDetail').getByText('任务详情',{exact:true}).waitFor();
const text=await page.locator('#businessTaskDetail').innerText();if(!text.includes('需要核对')||!text.includes('预算缺口'))throw Error('Durable task not restored');
if(await page.locator('#confirmBtn').count())throw Error('Reading a task restored confirmation');
if(process.env.BANK_TASK_SCREENSHOT)await page.evaluate(()=>window.scrollTo(0,0));
if(process.env.BANK_TASK_SCREENSHOT)await page.screenshot({path:process.env.BANK_TASK_SCREENSHOT,fullPage:true});
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
"""
        def check(base):
            p=subprocess.run([node,'-e',script],env=dict(os.environ,BANK_TASK_URL=base,BANK_TASK_ID=task['task_id']),capture_output=True,text=True,encoding='utf-8',timeout=50)
            self.assertEqual(p.returncode,0,p.stdout+p.stderr)
        self.serve(check);self.assertEqual(self.rt.bank.daily_transfer_cents,0);self.assertFalse(self.rt.pending)

if __name__=='__main__':unittest.main()
