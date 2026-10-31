import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import banking_agent as ba


class ElderModeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'c7.sqlite3'
        self.rt = ba.AgentRuntime(db_path=self.db, offline=True)

    def tearDown(self):
        self.rt.bank.close()
        self.rt.audit.db.close()
        self.tmp.cleanup()

    def post(self, path, body):
        handler = object.__new__(ba.Handler)
        handler.path = path
        handler.headers = {'Authorization': 'Bearer demo-user-session'}
        handler._client_key = lambda: 'c7-test'
        handler._body = lambda: body
        handler._send = lambda status, data, *args: (status, data)
        with patch.object(ba, 'runtime', self.rt), patch.object(ba, 'gateway', ba.AgentGateway(self.rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            return handler.do_POST()

    def test_configurable_terms_and_protected_facts(self):
        path = Path(self.tmp.name) / 'terms.json'
        path.write_text(json.dumps({'terms': {'触发增强认证策略': '为了保护您的钱，请再确认一次', '风险复核': '银行需要再核对一下', '妈妈': '不可替换的名字'}}), encoding='utf-8')
        layer = ba.PlainLanguage(path)
        self.assertEqual(layer.convert('触发增强认证策略，风险复核，给妈妈转 5,000.00 元', protected=['妈妈']),
                         '为了保护您的钱，请再确认一次，银行需要再核对一下，给妈妈转 5,000.00 元')
        self.assertEqual(layer.convert('处理中，不是成功；失败；5000 元'), '处理中，不是成功；失败；5000 元')

    def test_invalid_config_falls_back_without_inventing_numbers(self):
        path = Path(self.tmp.name) / 'bad.json'
        for contents in ('not-json', '{"terms":{"核验":"转账成功5000元"}}', '{"terms":[]}'):
            path.write_text(contents, encoding='utf-8')
            layer = ba.PlainLanguage(path)
            self.assertEqual(layer.configuration_status, 'fallback')
            self.assertNotIn('5000', layer.convert('核验'))
        self.assertEqual(ba.PlainLanguage(path.parent / 'missing').configuration_status, 'fallback')

    def test_presentation_only_does_not_change_policy_tokens_or_tools(self):
        response = self.rt.chat('给妈妈转 5000 元')
        response['message'] = '触发增强认证策略，金额 5,000 元，收款人妈妈'
        before = copy.deepcopy(response)
        plain = ba.present_plain_response(response, True)
        self.assertIn('为了保护您的钱，请再确认一次', plain['message'])
        self.assertIn('5,000 元', plain['message'])
        self.assertEqual(response, before)
        self.assertEqual(plain['plan'], before['plan'])
        self.assertEqual(plain['plan_view']['action_token'], before['plan_view']['action_token'])
        self.assertEqual(plain['presentation']['standard']['message'], before['message'])
        self.assertEqual(ba.present_plain_response(response, False)['message'], before['message'])

    def test_http_chat_confirm_verify_use_plain_layer(self):
        _, out = self.post('/api/chat', {'message': '给妈妈转 5000 元', 'elder_mode': True})
        p = out['plan']
        body = {'plan_id': p['plan_id'], 'action_token': p['consent_token'], 'elder_mode': True}
        _, confirmation = self.post('/api/confirm', body)
        self.assertTrue(confirmation['needs_auth'])
        self.assertNotIn('强验证', confirmation['message'])
        self.assertTrue(confirmation['presentation']['elder_mode'])
        _, verified = self.post('/api/verify', dict(body, code='123456'))
        self.assertNotIn('强验证', verified['message'])
        _, result = self.post('/api/confirm', body)
        self.assertEqual(result['result']['status'], 'SUCCESS')
        self.assertEqual(result['result']['results'][-1]['data']['amount_cents'], 500000)

    def test_mode_is_request_local_not_account_age_or_authority(self):
        for enabled in (True, False, 'true'):
            _, out = self.post('/api/chat', {'message': '给妈妈转 5000 元', 'elder_mode': enabled})
            self.assertEqual(out['presentation']['elder_mode'], enabled is True)
            self.assertTrue(out['plan']['requires_strong_auth'])
            self.assertEqual(out['plan']['risk_assessment']['risk_score'], 20)
        self.assertFalse(self.rt.bank.user_profiles['demo-user']['is_elderly'])

    def test_fraud_warning_and_service_guidance(self):
        _, out = self.post('/api/chat', {'message': '给妈妈转 1000 元，高收益稳赚', 'elder_mode': True})
        warning = out['presentation']['fraud_warning']
        self.assertEqual(warning['title'], '请先不要转账')
        self.assertIn('银行卡背面', warning['official_help'])
        self.assertIn('96110', warning['hotline_help'])
        self.assertIn('请先不要转账', out['message'])
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS)

    def test_processing_and_failure_remain_truthful(self):
        self.rt.bank.inject_fault('timeout')
        p = self.rt.chat('给妈妈转 200 元')['plan']
        _, out = self.post('/api/confirm', {'plan_id': p['plan_id'], 'action_token': p['consent_token'], 'elder_mode': True})
        self.assertEqual(out['result']['status'], 'PROCESSING')
        self.assertIn('银行还在确认这笔交易，请不要重复操作', out['message'])
        self.assertIsNone(out['presentation']['fraud_warning'])
        failed = ba.present_plain_response({'message': '操作失败，未扣款', 'result': {'status': 'FAILED'}}, True)
        self.assertEqual(failed['result']['status'], 'FAILED')
        self.assertEqual(failed['message'], '操作失败，未扣款')

    def test_customer_service_is_audited_queryable_idempotent_and_no_debit(self):
        body = {'user_id': 'demo-user', 'device_id': 'demo-device', 'request_id': 'support-test', 'elder_mode': True}
        code, out = self.post('/api/v1/customer-service', body)
        self.assertEqual(code, 200)
        self.assertEqual(out['status'], 'LOCAL_QUEUED')
        self.assertIn('尚未接通真人客服', out['message'])
        self.assertEqual(self.post('/api/v1/customer-service', body)[1]['event_id'], out['event_id'])
        events = self.rt.notifications.list_events('demo-user', out['trace_id'])
        self.assertEqual([e['event_type'] for e in events], ['customer_service.requested'])
        notices = self.rt.notifications.list_notifications('demo-user', out['trace_id'])
        self.assertEqual({n['channel'] for n in notices}, {'app_push', 'sms'})
        audit = self.rt.audit.list_for_trace(out['trace_id'])
        self.assertIn('customer_service.requested', [e['event_type'] for e in audit])
        self.assertTrue(self.rt.audit.verify_trace(out['trace_id'])['valid'])
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS)

    def test_existing_c2_subscriber_gets_new_service_event_but_guardian_does_not(self):
        self.rt.notifications.publish('device.risk', 'demo-user', 'old-trace', {}, 'old')
        _, out = self.post('/api/v1/customer-service', {'request_id': 'new-support'})
        self.assertEqual({n['channel'] for n in self.rt.notifications.list_notifications('demo-user', out['trace_id'])}, {'app_push', 'sms'})
        with self.assertRaises(ValueError):
            self.rt.notifications.subscribe('demo-user', 'guardian', 'family-child-demo', ['customer_service.requested'])

    def test_customer_service_request_survives_restart_and_invalid_input_is_rejected(self):
        body = {'request_id': 'restart-help'}
        _, out = self.post('/api/v1/customer-service', body)
        self.rt.bank.close(); self.rt.audit.db.close()
        self.rt = ba.AgentRuntime(db_path=self.db, offline=True)
        self.assertEqual(self.post('/api/v1/customer-service', body)[1]['event_id'], out['event_id'])
        for request in ({}, {'request_id': '../bad'}, {'request_id': 'x' * 129}):
            self.assertEqual(self.post('/api/v1/customer-service', request)[0], 400)

    def test_service_failure_does_not_claim_queued(self):
        with patch.object(self.rt.notifications, 'publish', side_effect=RuntimeError('private backend detail')):
            status, out = self.post('/api/v1/customer-service', {'request_id': 'failed-help'})
        self.assertEqual(status, 503)
        self.assertFalse(out['ok'])
        self.assertNotIn('private backend detail', json.dumps(out))
        self.assertNotEqual(out.get('status'), 'LOCAL_QUEUED')

    def test_frontend_contract_has_18px_risk_alert_and_one_click_help(self):
        html = (ROOT / 'app/static/index.html').read_text(encoding='utf-8')
        js = (ROOT / 'app/static/app.js').read_text(encoding='utf-8')
        css = (ROOT / 'app/static/style.css').read_text(encoding='utf-8')
        self.assertIn('id="customerServiceBtn"', html)
        self.assertIn('id="elderFraudAlert"', html)
        self.assertIn('role="alert"', html)
        self.assertIn('长辈模式', html)
        self.assertIn('/api/v1/customer-service', js)
        self.assertIn('elder_mode:elderMode', js)
        self.assertIn('font-size:18px', css)
        self.assertIn('body.elder-mode .bubble', css)
        self.assertIn('min-height:56px', css)
        self.assertIn('prefers-reduced-motion', css)

    def test_browser_mode_toggle_fraud_and_customer_service(self):
        """Optional dev-only browser QA; runtime remains zero-dependency."""
        import os
        import subprocess
        import threading
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('浏览器验收需指定 C7_NODE_BINARY 与 C7_PLAYWRIGHT_MODULE；不作为运行时依赖')
        script = r'''
const assert = require('node:assert/strict');
const {chromium} = require(process.env.C7_PLAYWRIGHT_MODULE);
(async () => {
 const browser = await chromium.launch({headless:true});
 try {
 const page = await browser.newPage({viewport:{width:1280,height:900}});
 const errors=[]; page.on('pageerror', e=>errors.push(e.message));
 let writes=0; page.on('request', r=>{if(r.method()==='POST')writes++;});
 const base=process.env.C7_TEST_URL;
 await page.goto(base);
 await page.waitForFunction(()=>document.querySelector('#elderToggle').getAttribute('aria-pressed')==='true');
 assert.equal(await page.locator('#elderToggle').textContent(),'长辈模式：开');
 assert.ok(await page.locator('.bubble').first().evaluate(e=>parseFloat(getComputedStyle(e).fontSize)>=18));
 assert.ok(await page.locator('#customerServiceBtn').evaluate(e=>e.getBoundingClientRect().height>=56));
 for (const selector of ['#elderToggle','#sendBtn','#voiceBtn','#voiceBtn span','#customerServiceBtn']) {
   assert.ok(await page.locator(selector).evaluate(e=>parseFloat(getComputedStyle(e).fontSize)>=18), selector+' 字号至少18px');
 }
 assert.equal(await page.locator('#securityCenter').isVisible(),false);
 await page.locator('#advancedToggle').click();
 assert.equal(await page.locator('#securityCenter').isVisible(),true);
 await page.locator('#advancedToggle').click();
 await page.locator('#elderToggle').click();
 await page.locator('#messageInput').fill('给妈妈转 5000 元');
 await page.locator('#sendBtn').click(); await page.locator('#confirmBtn').click();
 await page.locator('#authModal').waitFor({state:'visible'});
 assert.match(await page.locator('.message.bot .bubble').last().textContent(),/强验证/);
 await page.locator('#closeModal').click();
 const before=writes;
 await page.locator('#elderToggle').click();
 assert.match(await page.locator('.message.bot .bubble').last().textContent(),/为了保护您的钱，请再确认一次/);
 assert.equal(writes,before,'切换模式不得重新提交银行操作');
 assert.equal(await page.locator('#confirmBtn').count(),1);
 await page.locator('#messageInput').fill('给妈妈转 1000 元，高收益稳赚');
 await page.locator('#sendBtn').click();
 await page.locator('#elderFraudAlert').waitFor({state:'visible'});
 assert.equal(await page.locator('#elderFraudTitle').textContent(),'请先不要转账');
 assert.match(await page.locator('#elderHotlineHelp').textContent(),/96110/);
 assert.match(await page.locator('#elderOfficialHelp').textContent(),/银行卡背面/);
 assert.equal(await page.locator('#confirmBtn').count(),0);
 const titleStyle=await page.locator('#elderFraudTitle').evaluate(e=>({size:parseFloat(getComputedStyle(e).fontSize),color:getComputedStyle(e).color}));
 assert.ok(titleStyle.size>=30); assert.equal(titleStyle.color,'rgb(139, 16, 16)');
 await page.locator('#fraudHelpBtn').click();
 await page.waitForFunction(()=>document.querySelector('#customerServiceStatus').dataset.eventId);
 assert.match(await page.locator('#customerServiceStatus').textContent(),/尚未接通真人客服/);
 let events=await (await page.request.get(base+'/api/v1/notifications/events?user_id=demo-user')).json();
 assert.equal(events.items.filter(e=>e.event_type==='customer_service.requested').length,1);
 await page.locator('#customerServiceBtn').click();
 await page.waitForFunction(()=>!document.querySelector('#customerServiceBtn').disabled);
 events=await (await page.request.get(base+'/api/v1/notifications/events?user_id=demo-user')).json();
 assert.equal(events.items.filter(e=>e.event_type==='customer_service.requested').length,1);
 assert.equal(await page.locator('#elderFraudAlert').isVisible(),true,'请求客服不移除反诈提醒');
 if(process.env.C7_SCREENSHOT_DIR) await page.screenshot({path:process.env.C7_SCREENSHOT_DIR+'/c7-desktop.png',fullPage:true});
 await page.setViewportSize({width:390,height:844});
 assert.ok(await page.locator('.bubble').first().evaluate(e=>parseFloat(getComputedStyle(e).fontSize)>=18));
 assert.ok(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth),'手机端不得横向溢出');
 if(process.env.C7_SCREENSHOT_DIR) await page.screenshot({path:process.env.C7_SCREENSHOT_DIR+'/c7-mobile.png',fullPage:true});
 await page.locator('#elderToggle').click();
 await page.reload();
 assert.equal(await page.locator('#elderToggle').getAttribute('aria-pressed'),'false');
 await page.route('**/api/v1/customer-service', r=>r.abort());
 await page.locator('#customerServiceBtn').click();
 await page.waitForFunction(()=>!document.querySelector('#customerServiceBtn').disabled);
 assert.match(await page.locator('#customerServiceStatus').textContent(),/暂时无法确认/);
 assert.deepEqual(errors,[]);
 console.log(JSON.stringify({desktop:true,mobile:true,plain_toggle:true,fraud:true,customer_event:true,errors}));
 } finally {await browser.close();}
})().catch(e=>{console.error(e);process.exitCode=1;});
'''
        with patch.object(ba, 'runtime', self.rt), patch.object(ba, 'gateway', ba.AgentGateway(self.rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            server = ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                env = dict(os.environ, C7_TEST_URL='http://127.0.0.1:%d' % server.server_port)
                completed = subprocess.run([node, '-e', script], env=env, text=True, encoding='utf-8', capture_output=True, timeout=90)
                self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            finally:
                server.shutdown(); server.server_close(); thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
