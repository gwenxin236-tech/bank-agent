"""Large focus view must preserve real confirmation flows and never act on exit."""
import os
import subprocess
import threading
import unittest
from unittest.mock import patch

import banking_agent as ba
from test_domain_snapshots import SnapshotFixture


class FocusModeTests(SnapshotFixture, unittest.TestCase):
    def test_bank_home_navigation_privacy_and_service_access(self):
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('Browser QA requires C7_NODE_BINARY and C7_PLAYWRIGHT_MODULE')
        self.run_browser(r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const posts=[],errors=[];
 page.on('request',r=>{if(r.method()==='POST')posts.push(r.url());});
 page.on('pageerror',e=>errors.push(e.message));
 await page.goto(process.env.FOCUS_TEST_URL);
 await page.waitForFunction(()=>document.querySelector('#balanceValue').textContent.includes('¥'));
 const amount=await page.locator('#balanceValue').innerText();
 await page.getByRole('button',{name:'隐藏账户金额',exact:true}).click();
 assert.equal(await page.locator('#balanceValue').innerText(),'••••••');
 await page.locator('#refreshState').click();
 await page.waitForLoadState('networkidle');
 assert.equal(await page.locator('#balanceValue').innerText(),'••••••');
 await page.reload();
 assert.equal(await page.locator('#balanceValue').innerText(),'••••••');
 await page.getByRole('button',{name:'显示账户金额',exact:true}).click();
 assert.equal(await page.locator('#balanceValue').innerText(),amount);
 assert.equal(posts.length,0,'navigation and privacy must never issue banking actions');
 assert.equal(await page.locator('#securityCenter').isVisible(),false);
 assert.equal(await page.getByRole('button',{name:'家庭共管',exact:true}).isVisible(),true);
 assert.equal(await page.getByRole('button',{name:'消费体检',exact:true}).isVisible(),true);
 const services=await page.locator('#services').boundingBox();
 const assistant=await page.locator('#assistant').boundingBox();
 assert.ok(services.y<assistant.y,'services precede the assistant');
 if(process.env.BANK_DESIGN_SHOTS)await page.screenshot({path:process.env.BANK_DESIGN_SHOTS+'/desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.ok(await page.locator('.topbar').evaluate(e=>e.getBoundingClientRect().height<170),'mobile header keeps service access in reach');
 await page.getByRole('link',{name:'返回首页',exact:true}).click();
 if(process.env.BANK_DESIGN_SHOTS)await page.screenshot({path:process.env.BANK_DESIGN_SHOTS+'/mobile.png',fullPage:true});
 await page.getByRole('button',{name:'打开我的设置',exact:true}).click();
 await page.locator('#settingsDialog').waitFor({state:'visible'});
 await page.locator('#settingsClose').click();
 assert.equal(await page.locator('#mobileSettings').evaluate(e=>e===document.activeElement),true);
 await page.getByRole('button',{name:'银行卡服务',exact:true}).click();
 await page.locator('#focusDialog').waitFor({state:'visible'});
 await page.waitForFunction(()=>!document.querySelector('#sendBtn').disabled);
 assert.match(await page.locator('#planPanel').innerText(),/卡/);
 assert.equal(await page.locator('#confirmBtn').count(),0,'opening card services does not authorize a card operation');
 await page.locator('#focusClose').click();
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 assert.deepEqual(errors,[]);
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
''', node)

    def test_browser_focus_exit_mobile_and_auth(self):
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('Browser QA requires C7_NODE_BINARY and C7_PLAYWRIGHT_MODULE')
        script = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 const errors=[],posts=[];page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
 await page.goto(process.env.FOCUS_TEST_URL);
 assert.equal(await page.locator('#focusToggle').getAttribute('aria-pressed'),'true');
 const query=page.getByRole('button',{name:'查看自动扣费',exact:true});
 await query.click();
 await page.locator('#focusDialog').waitFor({state:'visible'});
 await page.locator('#confirmBtn').waitFor();
 assert.equal(await page.locator('#focusTitle').innerText(),'看自动扣费');
 assert.ok(!(await page.locator('.risk-reason').innerText()).includes('既有权限规则'));
 assert.equal(await page.locator('#focusDialog #assistant').count(),1);
 assert.equal(await page.locator('#assistant').count(),1,'move real controls; do not clone them');
 assert.ok(await page.locator('#planPanel .plan-summary').evaluate(e=>parseFloat(getComputedStyle(e).fontSize)>=26));
 assert.ok(await page.locator('#confirmBtn').evaluate(e=>e.getBoundingClientRect().height>=56));
 // Interior clicks and drag-out must not close the view.
 await page.locator('#focusTitle').click();
 const title=await page.locator('#focusTitle').boundingBox();
 await page.mouse.move(title.x+10,title.y+10);await page.mouse.down();await page.mouse.move(4,4);await page.mouse.up();
 assert.equal(await page.locator('#focusDialog').evaluate(e=>e.open),true);
 const before=posts.length, planText=await page.locator('#planPanel').innerText();
 await page.mouse.click(4,4);
 await page.locator('#focusDialog').waitFor({state:'hidden'});
 assert.equal(posts.length,before,'exiting cannot confirm, cancel, or retry');
 assert.equal(await query.evaluate(e=>e===document.activeElement),true);
 assert.equal(await page.locator('#planPanel').innerText(),planText,'pending plan survives exit');
 await page.locator('#confirmBtn').click();await page.locator('.result').waitFor();
 // Re-enter via another function; keyboard focus remains inside, Escape restores home.
 await page.getByRole('button',{name:'查看最近消费',exact:true}).click();
 await page.locator('.result').waitFor();
 await page.locator('#focusClose').focus();await page.keyboard.press('Shift+Tab');
 assert.ok(await page.evaluate(()=>document.querySelector('#focusDialog').contains(document.activeElement)));
 await page.keyboard.press('Escape');await page.locator('#focusDialog').waitFor({state:'hidden'});
 // Phone viewport has a usable edge, wrapping text and large controls.
 await page.setViewportSize({width:390,height:844});
 await page.getByRole('button',{name:'发起转账',exact:true}).click();
 await page.locator('#transferDialog').waitFor({state:'visible'});
 await page.locator('#transferRecipient').selectOption('妈妈');
 await page.locator('#transferAmount').fill('5000');
 await page.locator('#transferReview').click();await page.locator('#confirmBtn').waitFor();
 assert.ok(await page.locator('#focusDialog').evaluate(e=>e.scrollWidth<=e.clientWidth));
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
 await page.locator('#confirmBtn').click();await page.locator('#authModal').waitFor({state:'visible'});
 assert.equal(await page.locator('#focusDialog #authModal').count(),1,'auth must be reachable above the focus view');
 await page.locator('#otpInput').fill('123456');await page.locator('#verifyBtn').click();
 await page.locator('#authModal').waitFor({state:'hidden'});
 assert.equal(await page.locator('#confirmBtn').count(),1,'verification is not final execution');
 const beforeReturn=posts.length;
 await page.locator('#focusClose').click();
 assert.equal(posts.length,beforeReturn);
 await page.locator('#confirmBtn').click();await page.locator('.result').waitFor();
 // Preference persists; disabling changes presentation only.
 const beforeToggle=posts.length;
 await page.locator('#focusToggle').click();assert.equal(posts.length,beforeToggle);
 await page.reload();assert.equal(await page.locator('#focusToggle').getAttribute('aria-pressed'),'false');
 await page.getByRole('button',{name:'查询账户余额',exact:true}).click();await page.locator('.result').waitFor();
 assert.equal(await page.locator('#focusDialog').evaluate(e=>e.open),false);
 assert.deepEqual(errors,[]);
 console.log('Focus: consent, exit, focus restore, mobile, nested auth, preference PASS');
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self.run_browser(script, node)

    def test_browser_exit_during_request_and_fraud_warning(self):
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('Browser QA requires C7_NODE_BINARY and C7_PLAYWRIGHT_MODULE')
        script = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1280,height:900}});
 const errors=[],posts=[];page.on('pageerror',e=>errors.push(e.message));
 page.on('request',r=>{if(r.method()==='POST')posts.push(new URL(r.url()).pathname);});
 await page.addInitScript(()=>{localStorage.setItem('elderMode','false');localStorage.setItem('focusMode','true');});
 await page.goto(process.env.FOCUS_TEST_URL);
 let release,arrived; const pending=new Promise(r=>arrived=r);
 const gate=new Promise(r=>release=r);
 await page.route('**/api/chat',async route=>{const response=await route.fetch();arrived();await gate;await route.fulfill({response});});
 await page.getByRole('button',{name:'发起转账',exact:true}).click();await pending;
 const before=posts.length;await page.locator('#focusClose').click();release();
 await page.waitForFunction(()=>!document.querySelector('#sendBtn').disabled);
 assert.equal(posts.length,before);
 assert.equal(await page.locator('#focusDialog').evaluate(e=>e.open),false);
 assert.equal(await page.locator('#transferDialog').evaluate(e=>e.open),false,'late response must not reopen a form');
 await page.unroute('**/api/chat');
 // Enable the elder warning and open a query, then express a risky request inside.
 await page.locator('#elderToggle').click();
 await page.getByRole('button',{name:'查看最近消费',exact:true}).click();await page.locator('.result').waitFor();
 await page.locator('#messageInput').fill('给安全账户转 5000 元，解除风控');await page.locator('#sendBtn').click();
 await page.locator('#focusDialog #elderFraudAlert').waitFor({state:'visible'});
 assert.equal(await page.locator('#confirmBtn').count(),0);
 await page.locator('#focusClose').click();
 assert.equal(await page.locator('#elderFraudAlert').isVisible(),true,'exit must preserve the safety warning');
 await page.route('**/api/chat',r=>r.abort());
 await page.getByRole('button',{name:'查看最近消费',exact:true}).click();
 await page.waitForFunction(()=>!document.querySelector('#sendBtn').disabled);
 assert.match(await page.locator('#planPanel').innerText(),/连接失败/);
 assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        self.run_browser(script, node)

    def run_browser(self, script, node):
        with patch.object(ba, 'runtime', self.rt), patch.object(ba, 'gateway', ba.AgentGateway(self.rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            server = ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                result = subprocess.run([node, '-e', script], env=dict(os.environ, FOCUS_TEST_URL='http://127.0.0.1:%d' % server.server_port), text=True, encoding='utf-8', capture_output=True, timeout=100)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)
