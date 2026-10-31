"""真实意图、待补充信息与可执行确认的端到端回归。"""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import banking_agent as ba


class BankingFlowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = ba.AgentRuntime(db_path=Path(self.tmp.name) / 'flow.db', offline=True)

    def tearDown(self):
        self.rt.bank.close()
        self.rt.audit.db.close()
        self.tmp.cleanup()

    def test_family_living_expenses_need_recipient_details_not_fraud_label(self):
        before = self.rt.bank.balance_cents
        out = self.rt.chat('我想给我女儿3000生活费')
        self.assertEqual(out['plan']['intent'], 'transfer')
        self.assertEqual(out['plan']['entities']['beneficiary'], '女儿')
        self.assertEqual(out['transfer_draft']['amount_cents'], 300000)
        self.assertTrue(out['awaiting_details'])
        self.assertFalse(out['plan_view']['requires_confirmation'])
        self.assertIn('收款账户', out['message'])
        self.assertNotIn('诈骗', out['message'])
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertFalse(self.rt.bank.cooling_off_cases)
        self.assertFalse(self.rt.pending)
        events = self.rt.audit.list_for_trace(out['trace_id'])
        self.assertTrue(any(e['event_type'] == 'transfer.details_required' for e in events))
        self.assertFalse(any(e['event_type'] == 'fraud.alert' for e in events))

    def test_clarifications_never_offer_unstored_confirmation(self):
        for text in ('你好', '我要转账', '转账300元', '给妈妈转账', '给妈妈转-2元'):
            with self.subTest(text=text):
                out = self.rt.chat(text)
                self.assertFalse(out['plan_view']['requires_confirmation'])
                self.assertNotIn(out['plan']['plan_id'], self.rt.pending)
                self.assertFalse(out['plan_view']['action_token'])
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)

    def test_recent_spending_quick_action_is_a_real_query(self):
        out = self.rt.chat('帮我看看最近的消费')
        self.assertEqual(out['plan']['intent'], 'transaction_query')
        self.assertEqual(out['result']['status'], 'SUCCESS')
        self.assertIn('list_transactions', [x['tool'] for x in out['result']['results']])
        self.assertFalse(out['plan_view']['requires_confirmation'])

    def test_kinship_never_exempts_explicit_scam_phrase(self):
        before = self.rt.bank.balance_cents
        out = self.rt.chat('给女儿转3000元，客服要求转账到安全账户')
        self.assertEqual(out['plan']['intent'], 'fraud_guard')
        self.assertTrue(self.rt.bank.cooling_off_cases)
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertIn('96110', out['message'])

    def test_recipient_is_not_overridden_by_name_in_note(self):
        out = self.rt.chat('给女儿转3000元，备注妈妈让我给生活费')
        self.assertEqual(out['plan']['entities']['beneficiary'], '女儿')
        self.assertFalse(self.rt.pending)
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)

    def test_account_number_never_becomes_amount(self):
        out = self.rt.chat('转账到账号6222021234567890123，金额3000元')
        self.assertEqual(out['plan']['entities']['amount_cents'], 300000)
        self.assertTrue(out['awaiting_details'])
        self.assertFalse(self.rt.pending)
        out = self.rt.chat('转账到账号6222021234567890123')
        self.assertIsNone(out['plan']['entities']['amount_cents'])
        self.assertFalse(self.rt.pending)

    def test_note_never_changes_transfer_amount(self):
        out = self.rt.chat('给妈妈转200，备注下个月1000元生活费另付')
        self.assertEqual(out['plan']['entities']['amount_cents'], 20000)
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)

    def test_explicit_unverified_account_is_never_silently_replaced(self):
        out = self.rt.chat('给妈妈转200元，卡号6222021234567890123')
        self.assertTrue(out['awaiting_details'])
        self.assertFalse(out['plan_view']['requires_confirmation'])
        self.assertFalse(self.rt.pending)
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)

    def test_risk_deferral_is_not_presented_as_proven_scam(self):
        for i in range(2):
            self.rt.bank.transfer(20000, '妈妈', '', 'seed-%s' % i)
        # Existing configured score: new30 + amount20 + third30 => defer.
        out = self.rt.chat('给新收款人转3000元')
        self.assertEqual(out['plan']['intent'], 'transfer')
        self.assertEqual(out['result']['status'], 'DEFERRED')
        view = ba.present_plain_response(out, True)
        self.assertIsNone(view['presentation']['fraud_warning'])
        self.assertIn('核实', view['message'])
        self.assertFalse(self.rt.bank.cooling_off_cases)

    def test_supported_recipient_still_requires_auth_for_3000(self):
        before = self.rt.bank.balance_cents
        out = self.rt.chat('给李明转3000元，备注生活费')
        plan = out['plan']
        self.assertTrue(plan['requires_strong_auth'])
        self.assertIn(plan['plan_id'], self.rt.pending)
        challenged = self.rt.confirm(plan['plan_id'], plan['consent_token'])
        self.assertTrue(challenged['needs_auth'])
        self.assertEqual(self.rt.bank.balance_cents, before)
        verified = self.rt.verify(plan['plan_id'], plan['consent_token'], ba.POLICY['demo_otp'])
        self.assertTrue(verified['ok'])
        final = self.rt.confirm(plan['plan_id'], plan['consent_token'])
        self.assertEqual(final['result']['status'], 'SUCCESS')
        self.assertEqual(self.rt.bank.balance_cents, before - 300000)
        self.assertFalse(final['plan_view']['requires_confirmation'])
        self.assertTrue(any(e['event_type'] == 'tool.result' and e['payload']['step']['tool'] == 'create_transfer' for e in self.rt.audit.list_for_trace(out['trace_id'])))

    def test_hide_card_requests_never_become_loss_or_delete_operations(self):
        before = self.rt.bank.cards_query().data
        balance = self.rt.bank.balance_cents
        for text in ['不显示这张卡', '隐藏银行卡', '把这张卡隐藏，不要挂失', '不显示这张卡并注销']:
            with self.subTest(text=text):
                out = self.rt.chat(text)
                self.assertEqual(out['plan']['intent'], 'unknown')
                self.assertIn('显示与帮助设置', out['message'])
                self.assertIn('不挂失', out['message'])
                self.assertNotIn('result', out)
                self.assertFalse(out['plan_view']['requires_confirmation'])
                self.assertFalse(self.rt.pending)
                self.assertEqual(self.rt.bank.cards_query().data, before)
                self.assertEqual(self.rt.bank.balance_cents, balance)

    def test_plain_loss_request_still_requires_original_strong_auth(self):
        out = self.rt.chat('挂失银行卡')
        self.assertEqual(out['plan']['intent'], 'card_manage')
        self.assertTrue(out['plan']['requires_strong_auth'])
        self.assertEqual(self.rt.bank.cards_query().data['items'][0]['status'], '正常')


class BankingFlowBrowserTests(unittest.TestCase):
    def test_settings_discovery_card_visibility_and_storage_failure(self):
        import subprocess
        import threading
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('Run scripts/browser_qa.py --full for browser acceptance')
        script = r'''
const assert=require('node:assert/strict');
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
(async()=>{
 const browser=await chromium.launch({headless:true});
 try {
  const page=await browser.newPage({viewport:{width:390,height:844}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  const state=async()=> (await page.request.get(process.env.SETTINGS_URL+'/api/state',
    {headers:{Authorization:'Bearer demo-user-session'}})).json();
  await page.goto(process.env.SETTINGS_URL);
  const before=await state();
  await page.getByRole('button',{name:'显示与帮助设置',exact:true}).click();
  await page.locator('#settingsDialog').waitFor({state:'visible'});
  await page.locator('#visibleCardList li').waitFor();
  assert.equal(await page.locator('#visibleCardList li').count(),before.cards.items.length);
  if(process.env.T18_SCREENSHOT_DIR) await page.screenshot({path:process.env.T18_SCREENSHOT_DIR+'/settings-mobile.png'});
  await page.getByRole('button',{name:'仅隐藏银联白金卡',exact:true}).click();
  assert.equal(await page.locator('#visibleCardList li').count(),0);
  assert.match(await page.locator('#cardDisplayStatus').innerText(),/不挂失|不注销/);
  await page.reload(); await page.locator('#settingsBtn').click();
  await page.waitForFunction(()=>document.querySelector('#cardDisplayStatus').textContent.includes('隐藏1张'));
  assert.equal(await page.locator('#visibleCardList li').count(),0);
  assert.deepEqual((await state()).cards,before.cards);
  assert.equal((await state()).balance.available_cents,before.balance.available_cents);
  await page.locator('#showHiddenCards').check();
  await page.getByRole('button',{name:'恢复显示银联白金卡',exact:true}).click();
  assert.equal(await page.locator('#visibleCardList li').count(),1);
  // Local display preference cannot remove the card from backend truth.
  const storageKey='bankAgent:demo-user:cardDisplay:v1';
  await page.evaluate(k=>localStorage.setItem(k,'broken-json'),storageKey);
  await page.reload(); await page.locator('#settingsBtn').click();
  await page.locator('#visibleCardList li').waitFor();
  assert.match(await page.locator('#cardDisplayStatus').innerText(),/损坏/);
  await page.evaluate(()=>{Storage.prototype.setItem=function(){throw new Error('storage denied');};});
  await page.getByRole('button',{name:'仅隐藏银联白金卡',exact:true}).click();
  assert.match(await page.locator('#cardDisplayStatus').innerText(),/仅本次页面|无法保存/);
  await page.locator('#settingsElderToggle').uncheck();
  assert.equal(await page.locator('#elderToggle').getAttribute('aria-pressed'),'false');
  await page.locator('#settingsElderToggle').check();
  assert.equal(await page.locator('#elderToggle').getAttribute('aria-pressed'),'true');
  await page.locator('#settingsVoicePrivacy').uncheck();
  assert.equal(await page.locator('#voicePrivacyToggle').getAttribute('aria-checked'),'false');
  await page.locator('#settingsVoicePrivacy').check();
  assert.equal(await page.locator('#voicePrivacyToggle').getAttribute('aria-checked'),'true');
  for(const target of ['activity','securityCenter']) {
    await page.locator('[data-settings-target="'+target+'"]').click();
    await page.locator('#settingsDialog').waitFor({state:'hidden'});
    assert.equal(await page.locator('#advancedToggle').getAttribute('aria-expanded'),'true');
    assert.ok(await page.locator('#'+target).isVisible());
    assert.equal(await page.evaluate(()=>document.activeElement.id),target);
    await page.locator('#settingsBtn').click();
  }
  await page.locator('#settingsVoiceBtn').click();
  await page.locator('#voiceDialog').waitFor({state:'visible'});
  assert.equal(await page.locator('#voiceConsent').isChecked(),false);
  await page.locator('#voiceCancel').click();
  await page.locator('#settingsBtn').click();
  await page.locator('#settingsAuditBtn').click();
  await page.locator('#auditDrawer').waitFor({state:'visible'});
  await page.locator('#closeAudit').click();
  await page.locator('#settingsBtn').click();
  await page.locator('#settingsSupportBtn').click();
  await page.waitForFunction(()=>document.querySelector('#customerServiceStatus').dataset.caseId);
  await page.locator('#settingsBtn').click();
  await page.locator('#settingsClose').click();
  assert.equal(await page.evaluate(()=>document.activeElement.id),'settingsBtn');
  assert.ok(await page.locator('#resetBtn').isVisible());
  await page.route('**/api/state',r=>r.fulfill({status:503,json:{ok:false}}));
  await page.locator('#refreshState').click();
  await page.locator('#settingsBtn').click();
  await page.waitForFunction(()=>document.querySelector('#cardDisplayStatus').textContent.includes('无法读取'));
  assert.equal(await page.locator('#visibleCardList li').count(),0);
  await page.setViewportSize({width:1440,height:1000});
  if(process.env.T18_SCREENSHOT_DIR) await page.screenshot({path:process.env.T18_SCREENSHOT_DIR+'/settings-unavailable-desktop.png'});
  assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
  assert.deepEqual(errors,[]);
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        with tempfile.TemporaryDirectory() as tmp:
            rt = ba.AgentRuntime(db_path=Path(tmp)/'settings.db', offline=True)
            try:
                with patch.object(ba, 'runtime', rt), patch.object(ba, 'gateway', ba.AgentGateway(rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
                    with ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler) as server:
                        thread = threading.Thread(target=server.serve_forever, daemon=True)
                        thread.start()
                        try:
                            run = subprocess.run([node, '-e', script], env=dict(os.environ, SETTINGS_URL=f'http://127.0.0.1:{server.server_port}'), capture_output=True, text=True, encoding='utf-8', timeout=90)
                            self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
                        finally:
                            server.shutdown(); thread.join(timeout=5)
            finally:
                rt.recovery_scheduler.stop(); rt.bank.close(); rt.audit.db.close()

    def test_transfer_details_confirmation_stale_card_and_scroll(self):
        import subprocess
        import threading
        node, module = os.environ.get('C7_NODE_BINARY'), os.environ.get('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('需要 C7_NODE_BINARY / C7_PLAYWRIGHT_MODULE')
        script = r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
 const page=await browser.newPage({viewport:{width:1440,height:1000}});
 const errors=[];page.on('pageerror',e=>errors.push(e.message));
 await page.addInitScript(()=>localStorage.setItem('elderMode','false'));
 await page.goto(process.env.FLOW_TEST_URL);
 const state=async()=>page.evaluate(async()=> (await fetch('/api/state',{headers:{Authorization:'Bearer demo-user-session'}})).json());
 const before=(await state()).balance.available_cents;
 await page.getByRole('button',{name:'发起转账',exact:true}).click();
 await page.locator('#transferDialog').waitFor({state:'visible'});
 assert.equal(await page.locator('#transferRecipient').inputValue(),'');
 assert.equal(await page.locator('#transferAmount').inputValue(),'');
 assert.equal(await page.locator('#confirmBtn').count(),0);
 assert.equal((await state()).balance.available_cents,before);
 await page.locator('#transferRecipient').selectOption('李明');
 await page.locator('#transferAmount').fill('123.45');
 await page.locator('#transferNote').fill('生活费');
 await page.locator('#transferReview').click();
 await page.locator('#confirmBtn').waitFor();
 assert.match(await page.locator('#planPanel').innerText(),/李明/);
 assert.match(await page.locator('#planPanel').innerText(),/123\.45/);
 assert.match(await page.locator('#planPanel').innerText(),/139\*+9000/);
 assert.equal((await state()).balance.available_cents,before);
 await page.locator('#confirmBtn').click();
 await page.locator('.result').waitFor();
 assert.equal((await state()).balance.available_cents,before-12345);
  assert.equal(await page.locator('#planPanel .risk > span:last-child').innerText(),'黄色 · 已完成');
  assert.equal(await page.locator('#planPanel .risk').getAttribute('aria-label'),'风险等级：黄色；状态：已完成');
  assert.equal(await page.locator('#planPanel .risk-icon').innerText(),'!');
 assert.equal(await page.locator('#confirmBtn').count(),0);
 // Unknown and incomplete utterances cannot leave an old action button behind.
 await page.locator('#messageInput').fill('你好');await page.locator('#sendBtn').click();
 await page.waitForFunction(()=>!document.querySelector('#sendBtn').disabled);
 assert.equal(await page.locator('#confirmBtn').count(),0);
 await page.getByRole('button',{name:'查看最近消费',exact:true}).click();
 await page.locator('.result').waitFor();
 assert.equal(await page.locator('#confirmBtn').count(),0);
 await page.locator('#messageInput').fill('我想给我女儿3000生活费');await page.locator('#sendBtn').click();
 await page.locator('#transferDialog').waitFor({state:'visible'});
 await page.waitForFunction(()=>!document.querySelector('#transferReview').disabled);
 assert.equal(await page.locator('#transferAmount').inputValue(),'3000.00');
 assert.equal(await page.locator('#transferRecipient').inputValue(),'');
 assert.match(await page.locator('#transferRequestHint').innerText(),/女儿.*尚未绑定/);
 assert.equal((await state()).balance.available_cents,before-12345);
 if(process.env.FLOW_SCREENSHOT_DIR)await page.screenshot({path:process.env.FLOW_SCREENSHOT_DIR+'/transfer-details.png'});
 await page.locator('#transferClose').click();
 await page.locator('#messageInput').fill('给张伟转10元');await page.locator('#sendBtn').click();
 await page.locator('#confirmBtn').waitFor();
 await page.route('**/api/confirm',r=>r.fulfill({json:{ok:false,message:'操作计划不存在、已完成或已过期。'}}));
 await page.locator('#confirmBtn').click();
 await page.waitForFunction(()=>!document.querySelector('#sendBtn').disabled);
 assert.equal(await page.locator('#confirmBtn').count(),0,'不可重复点击失效计划');
 await page.locator('#elderToggle').click();await page.locator('#elderToggle').click();
 assert.equal(await page.locator('#confirmBtn').count(),0,'切换模式不可复活旧计划');
 for(const elder of [false,true]){
   if((await page.locator('#elderToggle').getAttribute('aria-pressed'))!==String(elder))await page.locator('#elderToggle').click();
   if(elder && await page.locator('#advancedToggle').getAttribute('aria-expanded')!=='true')await page.locator('#advancedToggle').click();
   for(const width of [1440,390]){
     await page.setViewportSize({width,height:1000});
     await page.locator('.security-center').scrollIntoViewIfNeeded();
     await page.waitForTimeout(300);
     assert.ok(await page.evaluate(()=>{
       const w=document.querySelector('.workspace').getBoundingClientRect();
       const s=document.querySelector('.security-center').getBoundingClientRect();
       return w.bottom<=s.top;
     }),'工作区不得盖住安全中心');
     assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=innerWidth));
     if(process.env.FLOW_SCREENSHOT_DIR)await page.screenshot({path:process.env.FLOW_SCREENSHOT_DIR+'/security-'+elder+'-'+width+'.png'});
   }
 }
 assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        with tempfile.TemporaryDirectory() as tmp:
            rt = ba.AgentRuntime(db_path=Path(tmp)/'browser.db', offline=True)
            try:
                with patch.object(ba, 'runtime', rt), patch.object(ba, 'gateway', ba.AgentGateway(rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
                    server = ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        run = subprocess.run([node, '-e', script], env=dict(os.environ, FLOW_TEST_URL='http://127.0.0.1:%s' % server.server_port), text=True, encoding='utf-8', capture_output=True, timeout=100)
                        self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
                    finally:
                        server.shutdown(); server.server_close(); thread.join(timeout=5)
            finally:
                rt.bank.close(); rt.audit.db.close()

    def test_screen_reader_full_journey_is_optional_browser_qa(self):
        """E7-B: full transfer journey is keyboard/read-screen friendly when configured."""
        import subprocess
        import threading
        node, module = os.environ.get('C7_NODE_BINARY'), os.environ.get('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('需要 C7_NODE_BINARY / C7_PLAYWRIGHT_MODULE；浏览器依赖仅用于可选 QA')
        script = r"""
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);
const assert=require('node:assert/strict');
(async()=>{
 const browser=await chromium.launch({headless:true});
 try{
  const page=await browser.newPage({viewport:{width:1280,height:900}});
  const errors=[]; page.on('pageerror',e=>errors.push(e.message));
  await page.addInitScript(()=>{localStorage.setItem('elderMode','false');localStorage.removeItem('voicePrivacy');});
  await page.goto(process.env.E7B_TEST_URL);
  assert.equal(await page.locator('#chatLog').getAttribute('role'),'log');
  assert.equal(await page.locator('#chatLog').getAttribute('aria-live'),'polite');
  assert.ok(await page.locator('#messageInput').getAttribute('aria-label') || await page.locator('label[for="messageInput"]').count());
  assert.equal(await page.locator('#voiceBtn').getAttribute('aria-label'),'语音输入');
  assert.equal(await page.locator('#planPanel').getAttribute('aria-live'),'polite');
  assert.equal(await page.locator('#a11yStatus').getAttribute('aria-live'),'polite');
  // 隐私开关位于语音弹窗内，先打开弹窗再验证其读屏可见性与偏好。
  await page.getByRole('button',{name:'语音输入',exact:true}).click();
  await page.locator('#voiceDialog').waitFor({state:'visible'});
  assert.equal(await page.locator('#voicePrivacyToggle').getAttribute('aria-checked'),'true');
  await page.evaluate(()=>window.__a11yAnnounce('转账金额 ¥5,000.00，收款账号 138****8000', false));
  const privateAnnouncement=await page.locator('#a11yStatus').textContent();
  assert.ok(!privateAnnouncement.includes('5,000') && !privateAnnouncement.includes('138****8000'));
  assert.match(privateAnnouncement,/金额|账号/);
  await page.locator('#voicePrivacyToggle').uncheck();
  assert.equal(await page.locator('#voicePrivacyToggle').getAttribute('aria-checked'),'false');
  await page.locator('#voicePrivacyToggle').check();
  await page.locator('#voiceCancel').click();
  await page.locator('#voiceDialog').waitFor({state:'hidden'});

  // 金额输入与错误提示：提示使用 live region，不依赖颜色。
  await page.getByRole('button',{name:'发起转账',exact:true}).click();
  await page.locator('#transferDialog').waitFor({state:'visible'});
  assert.equal(await page.locator('#transferDialog').getAttribute('aria-labelledby'),'transferTitle');
  assert.equal(await page.locator('#transferDialog').getAttribute('aria-describedby'),'transferScope');
  const fieldOrder=await page.locator('#transferDialog input, #transferDialog select, #transferDialog button').evaluateAll(es=>es.map(e=>e.id));
  assert.ok(fieldOrder.indexOf('transferRecipient')<fieldOrder.indexOf('transferAmount'));
  assert.ok(fieldOrder.indexOf('transferAmount')<fieldOrder.indexOf('transferReview'));
  await page.locator('#transferRecipient').selectOption('妈妈');
  await page.locator('#transferAmount').fill('0');
  await page.locator('#transferReview').click();
  assert.equal(await page.locator('#transferRequestHint').getAttribute('role'),'status');
  assert.match(await page.locator('#transferRequestHint').textContent(),/大于 0/);
  await page.locator('#transferClose').click();

  // 联系人消歧：同名候选必须展示后四位，不能自动选择。
  await page.locator('#messageInput').fill('给小王转100元');
  await page.locator('#sendBtn').click();
  await page.locator('#recipientDisambiguation').waitFor({state:'visible'});
  assert.match(await page.locator('#recipientDisambiguation').innerText(),/同名收款人/);
  assert.match(await page.locator('#recipientDisambiguation').innerText(),/后四位/);
  assert.equal(await page.locator('#recipientDisambiguation li').count(),2);
  assert.equal(await page.locator('#transferRecipient').inputValue(),'');
  await page.locator('#transferClose').click();

  // 确认 -> 强认证 -> 回执全链路。
  await page.locator('#messageInput').fill('给妈妈转5000元');
  await page.locator('#sendBtn').click();
  await page.locator('#confirmBtn').waitFor();
  const risk=page.locator('#planPanel .risk');
  assert.ok((await risk.innerText()).match(/红色|黄色|绿色/));
  assert.match(await risk.getAttribute('aria-label'),/风险等级/);
  assert.equal(await risk.locator('.risk-icon').getAttribute('aria-hidden'),'true');
  await page.locator('#confirmBtn').click();
  await page.locator('#authModal').waitFor({state:'visible'});
  assert.equal(await page.locator('#authModal').getAttribute('role'),'dialog');
  assert.equal(await page.locator('#otpInput').getAttribute('aria-label'),'验证码');
  assert.ok(await page.getByRole('button',{name:'验证后继续',exact:true}).count());
  await page.locator('#otpInput').fill('123456');
  await page.getByRole('button',{name:'验证后继续',exact:true}).click();
  await page.locator('#confirmBtn').waitFor();
  await page.locator('#confirmBtn').click();
  await page.locator('#planPanel .result').waitFor();
  assert.ok((await page.locator('#planPanel .result').innerText()).length>0);
  assert.match(await page.locator('#a11yStatus').textContent(),/已完成|确认|失败|核验/);
  assert.equal(await page.locator('#planPanel .risk').getAttribute('role'),'status');
  assert.deepEqual(errors,[]);
 }finally{await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
"""
        with tempfile.TemporaryDirectory() as tmp:
            rt = ba.AgentRuntime(db_path=Path(tmp)/'browser-e7b.db', offline=True)
            try:
                with patch.object(ba, 'runtime', rt), patch.object(ba, 'gateway', ba.AgentGateway(rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
                    server = ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler)
                    thread = threading.Thread(target=server.serve_forever, daemon=True)
                    thread.start()
                    try:
                        run = subprocess.run([node, '-e', script], env=dict(os.environ, E7B_TEST_URL='http://127.0.0.1:%s' % server.server_port), text=True, encoding='utf-8', capture_output=True, timeout=100)
                        self.assertEqual(run.returncode, 0, run.stdout+run.stderr)
                    finally:
                        server.shutdown(); server.server_close(); thread.join(timeout=5)
            finally:
                rt.bank.close(); rt.audit.db.close()
