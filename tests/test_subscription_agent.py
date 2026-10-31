import unittest
import banking_agent as ba
from unittest.mock import patch
from test_domain_snapshots import SnapshotFixture


class SubscriptionAgentTests(SnapshotFixture, unittest.TestCase):
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

