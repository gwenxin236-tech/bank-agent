import os,subprocess,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
import banking_agent as ba
class CrossDomainBrowserTests(unittest.TestCase):
 def test_cross_domain_graph_adoption_and_native_confirmation(self):
  node,module=os.getenv('C7_NODE_BINARY'),os.getenv('C7_PLAYWRIGHT_MODULE')
  if not node or not module:self.skipTest('Browser runtime required')
  script=r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{const b=await chromium.launch({headless:true});try{const page=await b.newPage({viewport:{width:1440,height:1000}});await page.goto(process.env.CROSS_URL);await page.locator('#messageInput').fill('给妈妈转1元');await page.locator('#sendBtn').click();await page.locator('#confirmBtn').waitFor();await page.locator('#dynamicDagPanel > summary').click();
const [proposed]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/dag/propose')),page.locator('#crossDomainDagPropose').click()]);const proposal=await proposed.json();if(!proposal.ok||!proposal.proposal.nodes.some(n=>n.tool==='check_funding_ready'))throw Error('No cross-domain condition');
const [adopted]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/dag/adopt')),page.locator('#dynamicDagAdopt').click()]);if(!(await adopted.json()).ok)throw Error('Adoption failed');await page.locator('#dynamicDagGraph').waitFor();
if(process.env.CROSS_SCREENSHOT){await page.evaluate(()=>window.scrollTo(0,0));await page.screenshot({path:process.env.CROSS_SCREENSHOT,fullPage:true});}
const [completed]=await Promise.all([page.waitForResponse(r=>r.url().endsWith('/api/confirm')),page.locator('#confirmBtn').click()]);if((await completed.json()).result.status!=='SUCCESS')throw Error('Native conditional transfer failed');
}finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1});
"""
  with tempfile.TemporaryDirectory() as tmp:
   rt=ba.AgentRuntime(db_path=Path(tmp)/'bank.sqlite3',offline=True)
   try:
    with patch.object(ba,'runtime',rt),patch.object(ba,'gateway',ba.AgentGateway(rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True),ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
     thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
     try:
      result=subprocess.run([node,'-e',script],env=dict(os.environ,CROSS_URL=f'http://127.0.0.1:{server.server_port}'),capture_output=True,text=True,encoding='utf-8',timeout=65);self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertEqual(rt.bank.daily_transfer_cents,100)
     finally:server.shutdown();thread.join(timeout=5)
   finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
if __name__=='__main__':unittest.main()
