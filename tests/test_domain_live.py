"""Domain analysis must reach users and agree with the execution plane."""
import copy
import unittest
from unittest.mock import patch
import banking_agent as ba
from test_domain_snapshots import SnapshotFixture


class DomainLiveTests(SnapshotFixture, unittest.TestCase):
    def test_missing_subscription_target_requires_read_confirmation_then_selection(self):
        with patch.object(self.rt.bank, 'subscriptions_query', wraps=self.rt.bank.subscriptions_query) as query, \
             patch.object(self.rt.bank, 'cancel_subscription', side_effect=AssertionError('unexpected write')):
            out = self.rt.chat('取消订阅')
            query.assert_not_called()
            self.assertTrue(out['plan_view']['requires_confirmation'])
            self.assertNotIn('result', out)
            self.assertEqual([s['tool'] for s in out['plan']['steps']], ['list_subscriptions'])
            done = self.finish_plan(out)
            self.assertEqual(query.call_count, 1)
            self.assertEqual(done['result']['status'], 'SUCCESS')
            self.assertTrue(done['plan_view']['domain_guidance']['choices'])
            self.assertIn('未取消', done['result_view']['user_message'])

    def test_questions_and_negations_cannot_become_domain_writes(self):
        phrases = ('查询视频会员是否已经取消', '帮我看看视频会员取消需要什么条件',
                   '视频会员能否取消', '请问怎么关闭自动续费', '视频会员取消后会退款吗',
                   '自动续费关掉了吗', '不要关掉视频会员',
                   '我不是要取消视频会员', '我没有要挂失银行卡，只查状态',
                   '我不是要挂失银行卡', '我并没有要求解挂银行卡', '银行卡挂失有什么影响')
        for text in phrases:
            with self.subTest(text=text), patch.object(self.rt.parser.llm, 'parse', side_effect=AssertionError('read guard must precede model')):
                out = self.rt.chat(text)
                self.assertFalse({'cancel_subscription','report_card_loss','unreport_card_loss'} &
                                 {s['tool'] for s in out['plan']['steps']})
                self.finish_plan(out)
        self.assertEqual(self.rt.bank.cards[0]['status'], '正常')
        self.assertEqual(self.rt.bank.subscriptions[0]['status'], 'active')

    def test_subscription_write_needs_an_action_request_not_a_mention(self):
        for text in ('我从未取消视频会员', '他让我取消视频会员', '听说取消视频会员很麻烦', '查询订阅'):
            with self.subTest(text=text), patch.object(self.rt.parser.llm, 'parse', side_effect=AssertionError('read must stay read')):
                out = self.rt.chat(text)
                self.assertNotIn('cancel_subscription', [s['tool'] for s in out['plan']['steps']])
        for text in ('请把视频会员取消', '视频会员帮我取消', '我想取消视频会员', '取消某视频会员，退款到账了吗',
                     '帮我关掉视频会员', '把视频会员关掉'):
            with self.subTest(text=text):
                out = self.rt.chat(text)
                self.assertIn('cancel_subscription', [s['tool'] for s in out['plan']['steps']])

    def test_explicit_unknown_card_name_is_not_defaulted(self):
        for text in ('挂失我的工商银行卡', '挂失我的建设银行储蓄卡', '挂失我的信用卡',
                     '挂失工商银行尾号6222的银行卡'):
            with self.subTest(text=text):
                out = self.rt.chat(text)
                self.assertFalse(out['plan']['entities'].get('card_id'))
                self.assertNotIn('report_card_loss', [s['tool'] for s in out['plan']['steps']])

    def test_exact_trusted_card_name_can_select_without_defaulting(self):
        self.rt.bank.cards.append(dict(self.rt.bank.cards[0],id='card_002',name='备用储蓄卡',last4='1234'))
        out = self.rt.chat('请帮我挂失备用储蓄卡')
        self.assertEqual(out['plan']['entities'].get('card_id'), 'card_002')
        self.assertTrue(out['plan_view']['requires_strong_auth'])

    def test_unloss_reminder_is_visible_before_confirmation_and_auth(self):
        self.rt.bank.cards[0]['status'] = 'reported_loss'
        out = self.rt.chat('帮我解挂银行卡')
        for word in ('已找回', '本人保管'):
            self.assertIn(word, str(out['plan_view']))
        pid, token = out['plan']['plan_id'], out['plan_view']['action_token']
        with patch.object(self.rt.bank, 'unreport_card_loss', side_effect=AssertionError('unexpected write')):
            challenged = self.rt.confirm(pid, token)
        self.assertTrue(challenged['needs_auth'])
        self.assertIn('本人保管', str(challenged['plan_view']))

    def test_explicit_unrecovered_card_never_gets_unloss_plan(self):
        self.rt.bank.cards[0]['status'] = 'reported_loss'
        with patch.object(self.rt.bank, 'unreport_card_loss', side_effect=AssertionError('unexpected write')):
            out = self.rt.chat('我的银行卡还没有找到，帮我解挂')
            self.assertNotIn('unreport_card_loss', [s['tool'] for s in out['plan']['steps']])
            done = self.finish_plan(out)
        self.assertIn('找回', str(done['plan_view']))
        self.assertFalse(any(c['request'] for c in done['plan_view']['domain_guidance']['choices']))

    def test_same_merchant_choices_show_price_and_block_identical_labels(self):
        extra = copy.deepcopy(self.rt.bank.subscriptions[0])
        extra.update(id='sub_003', amount_cents=9900)
        self.rt.bank.subscriptions.append(extra)
        done = self.complete('查询订阅')
        choices = done['plan_view']['domain_guidance']['choices']
        self.assertIn('25.00', choices[0]['label']); self.assertIn('99.00', choices[2]['label'])
        self.assertNotEqual(choices[0]['label'], choices[2]['label'])
        extra['amount_cents'] = 2500
        done = self.complete('查询订阅')
        choices = done['plan_view']['domain_guidance']['choices']
        self.assertFalse(choices[0]['request']); self.assertFalse(choices[2]['request'])

    def finish_plan(self, out):
        if out.get('result'):
            return out
        pid, token = out['plan']['plan_id'], out['plan_view']['action_token']
        done = self.rt.confirm(pid, token)
        if done.get('needs_auth'):
            verified = self.rt.verify(pid, token, '123456', credential_id=done['auth_challenge']['credential_id'])
            done = self.rt.confirm(pid, token, credential_id=verified['credential_id'])
        return done

    def test_explicit_negative_requests_never_write_even_after_confirmation(self):
        for text in ('不要取消视频会员，只查询扣费情况', '暂时不关闭视频会员自动续费',
                     '不要进行银行卡挂失，只查状态', '我不想把银行卡办理挂失',
                     '取消挂失申请，只查看卡片状态', '视频会员取消了吗'):
            with self.subTest(text=text):
                before = copy.deepcopy((self.rt.bank.cards, self.rt.bank.subscriptions))
                out = self.rt.chat(text)
                self.assertFalse({'cancel_subscription', 'report_card_loss', 'unreport_card_loss'} &
                                 {s['tool'] for s in out['plan']['steps']})
                self.finish_plan(out)
                self.assertEqual(before, (self.rt.bank.cards, self.rt.bank.subscriptions))

    def test_bank_card_mention_does_not_steal_payment(self):
        for text in ('给妈妈银行卡转100元', '给妈妈的银行卡转100元', '给妈妈转100元，备注视频会员费用'):
            out = self.rt.chat(text)
            self.assertEqual(out['plan']['intent'], 'transfer')
            self.assertEqual(out['plan']['entities']['beneficiary'], '妈妈')
            self.assertEqual(out['plan']['entities']['amount_cents'], 10000)
        done = self.finish_plan(out)
        self.assertEqual(done['result']['status'], 'SUCCESS')

    def test_card_tail_selection_and_visible_consent(self):
        self.rt.bank.cards.append(dict(self.rt.bank.cards[0], id='card_002', last4='1234'))
        out = self.rt.chat('挂失尾号1234的银行卡')
        self.assertEqual(out['plan']['entities']['card_id'], 'card_002')
        self.assertIn('1234', out['plan_view']['summary'])
        self.assertIn('1234', out['plan_view']['plain_summary'])
        self.assertTrue(out['plan_view']['requires_strong_auth'])
        done = self.finish_plan(out)
        self.assertEqual(done['result']['status'], 'SUCCESS')
        self.assertEqual(self.rt.bank.cards[0]['status'], '正常')
        self.assertEqual(self.rt.bank.cards[1]['status'], 'reported_loss')

    def test_duplicate_card_tail_requires_selection(self):
        self.rt.bank.cards.append(dict(self.rt.bank.cards[0], id='card_002'))
        out = self.rt.chat('挂失尾号6222的银行卡')
        self.assertFalse(out['plan']['entities'].get('card_id'))
        self.assertNotIn('report_card_loss', [s['tool'] for s in out['plan']['steps']])

    def test_query_completes_without_mfa_and_distinguishes_same_name_cards(self):
        self.rt.bank.cards.append(dict(self.rt.bank.cards[0], id='card_002', last4='1234'))
        out = self.rt.chat('查看卡片状态')
        self.assertFalse(out['plan_view']['requires_strong_auth'])
        done = self.finish_plan(out)
        labels = [c['label'] for c in done['plan_view']['domain_guidance']['choices']]
        self.assertIn('6222', labels[0]); self.assertIn('1234', labels[1])
        self.assertIn('正常', done['result_view']['user_message'])
        self.assertNotIn('下一步需要人工', done['result_view']['user_message'])
        self.assertNotIn('请说明是挂失还是解挂', str(done['plan_view']['domain_guidance']))

    def test_read_policy_cannot_authorize_card_write(self):
        decision = self.rt.policy.classify_tool('card_manage', {}, 'demo-user', 'report_card_loss')
        self.assertFalse(decision.get('authz_allowed', True))

    def test_repeat_loss_is_verified_noop_without_core_write(self):
        self.rt.bank.cards[0]['status'] = 'reported_loss'
        with patch.object(self.rt.bank, 'report_card_loss', side_effect=AssertionError('unexpected write')):
            done = self.finish_plan(self.rt.chat('帮我挂失银行卡'))
        self.assertEqual(done['result']['status'], 'SUCCESS')
        self.assertIn('无需重复挂失', done['result_view']['user_message'])
        events = self.rt.audit.list_for_trace(done['trace_id'])
        self.assertNotIn('card.loss.completed', [e['event_type'] for e in events])

    def test_repeat_unloss_is_verified_noop_without_core_write(self):
        with patch.object(self.rt.bank, 'unreport_card_loss', side_effect=AssertionError('unexpected write')):
            done = self.finish_plan(self.rt.chat('帮我解挂银行卡'))
        self.assertEqual(done['result']['status'], 'SUCCESS')
        self.assertIn('无需解挂', done['result_view']['user_message'])

    def test_subscription_plan_does_not_promise_success_before_review(self):
        out = self.rt.chat('取消云盘会员')
        self.assertNotIn('建议取消', out['plan_view']['summary'])
        self.assertNotIn('本次将停止', str(out['plan_view']))
        self.assertIn('核验', out['plan_view']['summary'])

    def test_subscription_receipt_does_not_claim_current_service_validity(self):
        done = self.complete('取消视频会员')
        self.assertNotIn('服务仍有效至', done['result_view']['user_message'])
        self.assertIn('服务商', done['result_view']['user_message'])

    def test_subscription_query_reports_record_not_current_entitlement(self):
        done = self.complete('查询自动续费订阅')
        self.assertNotIn('项服务有效的订阅', done['result_view']['user_message'])
        self.assertIn('服务商', done['result_view']['user_message'])

    def test_cancel_loss_is_unloss_not_loss(self):
        out = self.rt.chat('银行卡找回了，取消挂失')
        self.assertEqual(out['plan']['entities']['card_operation'], 'unreport_loss')
        self.assertTrue(out['plan_view']['requires_strong_auth'])

    def test_card_missing_is_not_a_negated_loss_request(self):
        out = self.rt.chat('银行卡不见了帮我挂失')
        self.assertEqual(out['plan']['entities'].get('card_operation'), 'report_loss')
        self.assertIn('report_card_loss', [s['tool'] for s in out['plan']['steps']])
        self.assertTrue(out['plan_view']['requires_strong_auth'])

    def test_indistinguishable_cards_have_no_action_button(self):
        self.rt.bank.cards.append(dict(self.rt.bank.cards[0], id='card_002'))
        done = self.finish_plan(self.rt.chat('查看卡片状态'))
        self.assertTrue(all(not c['request'] for c in done['plan_view']['domain_guidance']['choices']))

    def test_noop_state_race_never_falls_back_to_write(self):
        self.rt.bank.cards[0]['status'] = 'reported_loss'
        out = self.rt.chat('挂失银行卡')
        with patch.object(self.rt.bank, 'card_status_query', return_value=ba.ToolResult(True, 'verify_card_status', {'status':'正常'})), \
             patch.object(self.rt.bank, 'report_card_loss', side_effect=AssertionError('unexpected write')):
            done = self.finish_plan(out)
        self.assertEqual(done['result']['status'], 'FAILED')
        self.assertIn('状态已变化', done['result_view']['user_message'])

    def complete(self, text):
        out = self.rt.chat(text)
        return self.rt.confirm(out['plan']['plan_id'], out['plan_view']['action_token'])

    def test_subscription_query_shows_reviewed_guidance_after_confirmation(self):
        out = self.rt.chat('查询自动续费订阅')
        self.assertFalse(out['plan_view'].get('domain_guidance'))
        done = self.rt.confirm(out['plan']['plan_id'], out['plan_view']['action_token'])
        guidance = done['plan_view']['domain_guidance']
        self.assertEqual(guidance['domain'], 'subscription')
        self.assertTrue(guidance['notes'])
        self.assertEqual(len(guidance['choices']), 2)
        self.assertNotIn('action_token', str(guidance))

    def test_unavailable_subscription_cannot_be_cancelled(self):
        before = copy.deepcopy(self.rt.bank.subscriptions)
        done = self.complete('取消云盘会员')
        self.assertNotEqual(done['result']['status'], 'SUCCESS')
        self.assertEqual(before, self.rt.bank.subscriptions)
        self.assertIn('核实', done['result_view']['user_message'])
        self.assertTrue(done['plan_view']['domain_guidance']['notes'])

    def test_execution_rechecks_changed_terminability(self):
        out = self.rt.chat('取消视频会员')
        self.rt.bank.subscriptions[0]['authorization_layer']['terminable'] = False
        done = self.rt.confirm(out['plan']['plan_id'], out['plan_view']['action_token'])
        self.assertNotEqual(done['result']['status'], 'SUCCESS')
        self.assertEqual(self.rt.bank.subscriptions[0]['status'], 'active')

    def test_negated_card_write_is_query(self):
        for text in ('不要挂失银行卡，我只想查一下状态', '先别解挂，我只查状态'):
            out = self.rt.chat(text)
            self.assertFalse(any(s['tool'] in ('report_card_loss','unreport_card_loss') for s in out['plan']['steps']))

    def test_ambiguous_card_never_defaults_to_first(self):
        out = self.rt.chat('我的第二张卡丢了，帮我挂失')
        self.assertFalse(out['plan']['entities'].get('card_id'))
        self.assertFalse(any(s['tool']=='report_card_loss' for s in out['plan']['steps']))

    def test_explicit_card_selection_is_bound_and_requires_auth(self):
        self.rt.bank.cards.append({'id':'card_002','name':'备用卡','status':'正常','last4':'1234'})
        out = self.rt.chat('挂失卡片编号card_002')
        self.assertEqual(out['plan']['entities']['card_id'], 'card_002')
        self.assertTrue(out['plan_view']['requires_strong_auth'])
        self.assertTrue(self.rt.confirm(out['plan']['plan_id'],out['plan_view']['action_token'])['needs_auth'])

    def test_unknown_card_status_never_mutates(self):
        self.rt.bank.cards[0]['status'] = 'unknown'
        result = self.rt.bank.report_card_loss('card_001','demo-user','test-unknown')
        self.assertFalse(result.ok)
        self.assertEqual(self.rt.bank.cards[0]['status'], 'unknown')

    def test_explicit_subscription_choice_creates_fresh_confirmation(self):
        out = self.rt.chat('取消订阅编号sub_001')
        self.assertEqual(out['plan']['entities']['subscription_id'], 'sub_001')
        self.assertTrue(out['plan_view']['requires_confirmation'])
        self.assertEqual(self.rt.bank.subscriptions[0]['status'], 'active')

    def test_agent_failure_blocks_write_without_retry(self):
        out = self.rt.chat('取消视频会员')
        with patch.object(self.rt.domain_agents, 'create', side_effect=ValueError('private')):
            done = self.rt.confirm(out['plan']['plan_id'],out['plan_view']['action_token'])
        self.assertNotEqual(done['result']['status'], 'SUCCESS')
        self.assertEqual(self.rt.bank.subscriptions[0]['status'],'active')
        self.assertNotIn('private', str(done))

    def test_completed_cancellation_does_not_show_unexecuted_advice(self):
        done = self.complete('取消视频会员')
        self.assertEqual(done['result']['status'], 'SUCCESS')
        guidance = done['plan_view']['domain_guidance']
        self.assertNotIn('当前尚未取消', ' '.join(guidance['notes']))
        self.assertEqual(guidance['choices'], [])

    def test_browser_domain_selection_auth_noop_and_negative_requests(self):
        import os
        import subprocess
        import threading
        node, module = os.getenv('C7_NODE_BINARY'), os.getenv('C7_PLAYWRIGHT_MODULE')
        if not node or not module:
            self.skipTest('Browser QA requires C7_NODE_BINARY and C7_PLAYWRIGHT_MODULE')
        self.rt.bank.cards.append(dict(self.rt.bank.cards[0], id='card_002', last4='1234'))
        extra = copy.deepcopy(self.rt.bank.subscriptions[0])
        extra.update(id='sub_003', amount_cents=9900)
        self.rt.bank.subscriptions.append(extra)
        script = r'''
const {chromium}=require(process.env.C7_PLAYWRIGHT_MODULE);const assert=require('node:assert/strict');
(async()=>{const browser=await chromium.launch({headless:true});try{
 const p=await browser.newPage();const errors=[];p.on('pageerror',e=>errors.push(e.message));
 await p.goto(process.env.DOMAIN_QA_URL);
 async function send(text){await p.waitForFunction(()=>!busy);await p.locator('#messageInput').fill(text);await p.locator('#sendBtn').click();await p.waitForFunction(()=>!busy);}
 async function authenticate(){await p.locator('#confirmBtn').click();await p.locator('#otpInput').fill('123456');await p.locator('#verifyBtn').click();await p.waitForFunction(()=>!busy);await p.locator('#confirmBtn').click();await p.waitForFunction(()=>!busy&&currentResultView?.status==='SUCCESS');}
 await send('查看卡片状态');
 assert.equal(await p.locator('#confirmBtn').count(),0);assert.equal(await p.locator('#authModal').isVisible(),false);
 const choices=p.locator('.domain-choice');assert.equal(await choices.count(),2);
 assert.match(await choices.nth(0).innerText(),/6222/);assert.match(await choices.nth(1).innerText(),/1234/);
 await choices.nth(1).getByRole('button',{name:'申请挂失',exact:true}).click();await p.waitForFunction(()=>!busy);
 assert.match(await p.locator('#planPanel').innerText(),/1234/);
 await authenticate();assert.match(await p.locator('#chatLog').innerText(),/1234.*已挂失/);
 await send('挂失尾号1234的银行卡');await authenticate();assert.match(await p.locator('#chatLog').innerText(),/无需重复挂失/);
 await send('不要进行银行卡挂失，只查状态');assert.equal(await p.locator('#confirmBtn').count(),0);
 await send('不要取消视频会员，只查询扣费情况');await p.locator('#confirmBtn').click();await p.waitForFunction(()=>!busy&&currentResultView?.status==='SUCCESS');
 assert.match(await p.locator('#domainGuidance').innerText(),/某视频会员/);
 await send('取消云盘会员');assert.doesNotMatch(await p.locator('#planPanel').innerText(),/建议取消|本次将停止/);
 await p.locator('#confirmBtn').click();await p.waitForFunction(()=>!busy&&currentResultView?.status==='FAILED');
 assert.match(await p.locator('#chatLog').innerText(),/未执行/);
 await send('给妈妈银行卡转100元');assert.match(await p.locator('#planPanel').innerText(),/妈妈/);
 assert.match(await p.evaluate(()=>currentPlan.summary),/向妈妈转账/);
 assert.equal(await p.locator('#confirmBtn').count(),0);
 await send('解挂尾号1234的银行卡');assert.match(await p.locator('#planPanel').innerText(),/已找回且由本人保管/);await authenticate();assert.match(await p.locator('#chatLog').innerText(),/1234.*已解挂/);
 await send('给妈妈银行卡转100元');
 await p.locator('#confirmBtn').click();await p.waitForFunction(()=>!busy&&currentResultView?.status==='SUCCESS');
 assert.match(await p.locator('#chatLog').innerText(),/已向妈妈转账/);
 await send('我没有要挂失银行卡，只查状态');assert.equal(await p.locator('#confirmBtn').count(),0);
 await send('挂失我的工商银行卡');assert.equal(await p.locator('#confirmBtn').count(),0);
 await send('查询视频会员是否已经取消');await p.locator('#confirmBtn').click();await p.waitForFunction(()=>!busy&&currentResultView?.status==='SUCCESS');
 await send('取消订阅');assert.equal(await p.locator('#domainGuidance').count(),0);assert.match(await p.locator('#planPanel').innerText(),/本次不取消订阅/);
 await p.locator('#confirmBtn').click();await p.waitForFunction(()=>!busy&&currentResultView?.status==='SUCCESS');
 assert.match(await p.locator('#chatLog').innerText(),/本次未取消任何订阅/);
 const selected=p.locator('.domain-choice').filter({hasText:'99.00'});assert.equal(await selected.count(),1);
 await selected.getByRole('button',{name:'申请停止扣费',exact:true}).click();await p.waitForFunction(()=>!busy);
 assert.match(await p.locator('#planPanel').innerText(),/99.00/);
 await p.locator('#confirmBtn').click();await p.waitForFunction(()=>!busy&&currentResultView?.status==='SUCCESS');
 assert.match(await p.locator('#chatLog').innerText(),/已确认停止未来扣费/);
 assert.deepEqual(errors,[]);
 if(process.env.DOMAIN_QA_SCREENSHOT)await p.screenshot({path:process.env.DOMAIN_QA_SCREENSHOT,fullPage:true});
}finally{await browser.close()}})().catch(e=>{console.error(e);process.exit(1)});
'''
        with patch.object(ba, 'runtime', self.rt), patch.object(ba, 'gateway', ba.AgentGateway(self.rt)), \
             patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            with ba.ThreadingHTTPServer(('127.0.0.1', 0), ba.Handler) as server:
                thread = threading.Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    run = subprocess.run([node, '-e', script], env=dict(os.environ, DOMAIN_QA_URL=f'http://127.0.0.1:{server.server_port}'),
                                         capture_output=True, text=True, encoding='utf-8', timeout=120)
                    self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
                    self.assertEqual(self.rt.bank.cards[0]['status'], '正常')
                    self.assertEqual(self.rt.bank.cards[1]['status'], '正常')
                    self.assertEqual(self.rt.bank.subscriptions[0]['status'], 'active')
                    self.assertEqual(self.rt.bank.subscriptions[2]['status'], 'cancelled')
                finally:
                    server.shutdown(); thread.join(timeout=5)
