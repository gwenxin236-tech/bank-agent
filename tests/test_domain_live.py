"""Domain analysis must reach users and agree with the execution plane."""
import copy
import unittest
from unittest.mock import patch
import banking_agent as ba
from test_domain_snapshots import SnapshotFixture


class DomainLiveTests(SnapshotFixture, unittest.TestCase):
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
