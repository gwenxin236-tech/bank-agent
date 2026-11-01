import os,subprocess,threading,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import banking_agent as ba

class EffectBrowserTests(unittest.TestCase):
    def test_single_effect_scope_is_visible_before_native_confirmation(self):
        node,module=os.getenv('C7_NODE_BINARY'),os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:self.skipTest('C7_NODE_BINARY/C7_PLAYWRIGHT_MODULE required')
        script=r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{const browser=await chromium.launch({headless:true});try{
const page=await browser.newPage({viewport:{width:1280,height:1000}});await page.goto(process.env.EFFECT_URL);
await page.locator('#messageInput').fill('给妈妈转1元');const wait=page.waitForResponse(r=>r.url().endsWith('/api/chat')&&r.request().method()==='POST');await page.locator('#sendBtn').click();await wait;
await page.locator('#paymentEffectScope').waitFor();const text=await page.locator('#paymentEffectScope').innerText();
if(!text.includes('仅本次转账')||!text.includes('一次性')||!text.includes('不代表已经授权'))throw Error('Missing scope or authority boundary');
await page.locator('#confirmBtn').waitFor();const done=page.waitForResponse(r=>r.url().endsWith('/api/confirm')&&r.request().method()==='POST');await page.locator('#confirmBtn').click();const body=await(await done).json();
if(body.result.status!=='SUCCESS')throw Error('Native confirmation chain did not complete');
}finally{await browser.close();}})().catch(e=>{console.error(e);process.exitCode=1});
"""
        with tempfile.TemporaryDirectory() as tmp:
            rt=ba.AgentRuntime(db_path=Path(tmp)/'effects.sqlite3',offline=True)
            try:
                with patch.object(ba,'runtime',rt),patch.object(ba,'gateway',ba.AgentGateway(rt)),patch.object(ba.http_rate_limiter,'allow',return_value=True),ba.ThreadingHTTPServer(('127.0.0.1',0),ba.Handler) as server:
                    thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
                    try:
                        p=subprocess.run([node,'-e',script],env=dict(os.environ,EFFECT_URL=f'http://127.0.0.1:{server.server_port}'),capture_output=True,text=True,encoding='utf-8',timeout=50)
                        self.assertEqual(p.returncode,0,p.stdout+p.stderr);self.assertEqual(rt.bank.daily_transfer_cents,100)
                    finally:server.shutdown();thread.join(timeout=5)
            finally:rt.recovery_scheduler.stop();rt.bank.close();rt.audit.db.close()

if __name__=='__main__':unittest.main()
