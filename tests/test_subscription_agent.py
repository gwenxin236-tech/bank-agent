import unittest
import copy
import banking_agent as ba
from unittest.mock import patch
from test_domain_snapshots import SnapshotFixture


class SubscriptionAgentTests(SnapshotFixture, unittest.TestCase):
    def selected(self, *, debit='active', refund='unknown', terminable=True):
        result = copy.deepcopy(self.rt.bank.subscriptions_query())
        result.data['items'][0]['authorization_layer'].update(debit_permission=debit, terminable=terminable)
        result.data['items'][0]['service_layer']['refund_status'] = refund
        return self.agent('subscription', result, self.ctx(entities={'subscription_id':'sub_001', 'operation':'cancel'}))

    def test_explanation_separates_future_debits_service_and_available_money(self):
        ctx, agent = self.selected(debit='stopped')
        finding = agent.analyze(ctx)
        text = ' '.join(finding.recommendations)
        self.assertIn('未来扣费已停止', text)
        self.assertIn('服务', text)
        self.assertIn('可用余额', text)
        self.assertIn('debit_already_stopped', finding.risk_signals)
        self.assertEqual(agent.propose(ctx, finding).suggested_steps, ())

    def test_blocked_cancellation_explains_reason_and_offers_handoff(self):
        ctx, agent = self.selected(terminable=False)
        finding = agent.analyze(ctx)
        self.assertIn('cancellation_unavailable', finding.risk_signals)
        self.assertIn({'action':'human_handoff'}, agent.propose(ctx, finding).to_dict()['user_options'])

    def test_unknown_debit_requires_verified_state_before_proposal(self):
        ctx, agent = self.selected(debit='unknown')
        finding = agent.analyze(ctx)
        self.assertIn('debit_state', finding.missing_fields)
        self.assertEqual(agent.propose(ctx, finding).suggested_steps, ())

    def test_unselected_refund_does_not_contaminate_selected_explanation(self):
        ctx, agent = self.selected(refund='pending')
        finding = agent.analyze(ctx)
        self.assertNotIn('refund_unverified', finding.risk_signals)
        self.assertIn('refund_pending', finding.risk_signals)
        self.assertIn('处理中', ' '.join(finding.recommendations))

    def test_empty_and_failed_query_have_different_explanations(self):
        for ok, expected in ((True, '未发现'), (False, '未能核实')):
            ctx, agent = self.agent('subscription', ba.ToolResult(ok, 'list_subscriptions', {'items':[]}))
            finding = agent.analyze(ctx)
            self.assertIn(expected, ' '.join(finding.recommendations))
            self.assertEqual(agent.propose(ctx, finding).suggested_steps, ())

    def test_duplicate_id_is_not_silently_selected(self):
        result = self.rt.bank.subscriptions_query()
        result.data['items'][1]['id'] = 'sub_001'
        ctx, agent = self.agent('subscription', result, self.ctx(entities={'subscription_id':'sub_001','operation':'cancel'}))
        self.assertEqual(agent.propose(ctx, agent.analyze(ctx)).suggested_steps, ())
        self.assertIn('subscription_identity_ambiguous', agent.analyze(ctx).risk_signals)

    def test_supervisor_accepts_explanations_and_facts_remain_unchanged(self):
        from supervisor import Supervisor
        from domain_agent_registry import DOMAIN_AGENT_REGISTRY
        for debit in ('active', 'stopped', 'unknown'):
            ctx, agent = self.selected(debit=debit)
            before = agent.snapshot
            finding = agent.analyze(ctx)
            self.assertEqual(finding.facts, before.facts)
            self.assertEqual(finding.evidence, before.evidence)
            self.assertEqual(finding, agent.analyze(ctx))
            Supervisor(DOMAIN_AGENT_REGISTRY).observe(ctx, 'subscription', before)

    def test_three_layers_unknown_refund_and_missing_id_no_silent_selection(self):
        ctx, agent = self.agent('subscription', self.rt.bank.subscriptions_query())
        facts = {f['name']:f['value'] for f in agent.analyze(ctx).facts}
        self.assertEqual(facts['subscriptions'][0]['debit_state'], 'active')
        self.assertEqual(facts['subscriptions'][0]['service_state'], 'active')
        self.assertEqual(facts['subscriptions'][0]['refund_state'], 'unknown')
        proposal = agent.propose(ctx, agent.analyze(ctx))
        self.assertEqual(proposal.suggested_steps, ())
        self.assertEqual(len(proposal.user_options), 2)

    def test_exact_selected_subscription_suggests_confirmed_action_only(self):
        ctx = self.ctx(entities={'subscription_id':'sub_001','operation':'cancel'})
        ctx, agent = self.agent('subscription', self.rt.bank.subscriptions_query(), ctx)
        with patch.object(self.rt.bank, 'cancel_subscription', side_effect=AssertionError('write')):
            proposal = agent.propose(ctx, agent.analyze(ctx))
        self.assertEqual(proposal.suggested_steps[0]['params']['subscription_id'], 'sub_001')
        self.assertTrue(proposal.required_confirmations)
        self.assertEqual(self.rt.bank.subscriptions[0]['status'], 'active')

    def test_unknown_target_nonterminable_and_failed_reads_do_not_cancel(self):
        for sid in ('missing', 'sub_002'):
            ctx = self.ctx(entities={'subscription_id':sid,'operation':'cancel'})
            ctx, agent = self.agent('subscription', self.rt.bank.subscriptions_query(), ctx)
            self.assertEqual(agent.propose(ctx, agent.analyze(ctx)).suggested_steps, ())
        ctx, agent = self.agent('subscription', ba.ToolResult(False, 'list_subscriptions'))
        self.assertEqual(agent.propose(ctx, agent.analyze(ctx)).suggested_steps, ())

