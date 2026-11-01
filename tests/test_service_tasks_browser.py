import os,subprocess,tempfile,threading,unittest
from pathlib import Path
from unittest.mock import patch
import banking_agent as ba
class ServiceBrowserTests(unittest.TestCase):
 def test_dispute_handoff_and_funding_recheck_page(self):
  node,module=os.getenv('C7_NODE_BINARY'),os.getenv('C7_PLAYWRIGHT_MODULE')
  if not node or not module:self.skipTest('Browser runtime required')
  script=r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{const b=await chromium.launch({headless:true});try{const p=await b.newPage({viewport:{width:1440,height:1050}});await p.goto(process.env.SERVICE_URL);await p.locator('#serviceTransaction option').nth(1).waitFor({state:'attached'});await p.locator('#serviceTransaction').selectOption({index:1});
const [opened]=await Promise.all([p.waitForResponse(r=>r.url().endsWith('/api/v1/service-tasks/dispute')),p.locator('#openDisputeBtn').click()]);const d=await opened.json();if(!d.ok||d.service_task.official_case_id!==null)throw Error('Dispute record not local');
await p.locator('#serviceHandoffBtn').click();await p.locator('#serviceTaskDetail').getByText('待人工领取',{exact:false}).waitFor();await p.reload();await p.locator('[data-service-task="'+d.service_task.task_id+'"]').click();await p.locator('#serviceTaskDetail').getByText('银行正式案件号：尚未取得；退款尚未核实。',{exact:true}).waitFor();
await p.locator('#serviceFundingAmount').fill('1000');await p.locator('#serviceFundingReserve').fill('2000');await p.locator('#serviceFundingDeadline').fill('2030-11-01T18:00');const [funded]=await Promise.all([p.waitForResponse(r=>r.url().endsWith('/api/v1/service-tasks/funding')),p.locator('#openFundingBtn').click()]);const f=await funded.json();if(!f.ok||!f.service_task.constraints.no_borrow)throw Error('Funding constraints lost');await p.locator('#serviceRecheckBtn').click();await p.locator('#serviceTaskDetail').getByText('不会自动办理付款',{exact:false}).waitFor();
if(process.env.SERVICE_SCREENSHOT){await p.evaluate(()=>window.scrollTo(0,0));await p.screenshot({path:process.env.SERVICE_SCREENSHOT,fullPage:true});}
}finally{await b.close();}})().catch(e=>{console.error(e);process.exitCode=1});
"""
  with tempfile.TemporaryDirectory() as tmp:
   rt=ba.AgentRuntime(db_path=Path(tmp)/'bank.sqlite3',offline=True)
   try:
    with patch.object(ba,'runtime',rt),patch.object(ba,'gateway',ba.AgentGateway(rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True),ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
     thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
     try:
      result=subprocess.run([node,'-e',script],env=dict(os.environ,SERVICE_URL=f'http://127.0.0.1:{server.server_port}'),capture_output=True,text=True,encoding='utf-8',timeout=65);self.assertEqual(result.returncode,0,result.stdout+result.stderr);self.assertEqual(rt.bank.daily_transfer_cents,0)
     finally:server.shutdown();thread.join(timeout=5)
   finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()
if __name__=='__main__':unittest.main()
