import unittest
import banking_agent as ba
from unittest.mock import patch
from test_domain_snapshots import SnapshotFixture


class CardAgentTests(SnapshotFixture, unittest.TestCase):
    def test_status_facts_no_raw_card_and_strong_auth_suggestion(self):
        ctx = self.ctx(entities={'card_id':'card_001','card_operation':'report_loss'})
        ctx, agent = self.agent('card', self.rt.bank.cards_query(), ctx)
        finding = agent.analyze(ctx)
        self.assertNotIn('6222021234561234', str(finding.to_dict()))
        with patch.object(self.rt.bank, 'report_card_loss', side_effect=AssertionError('write')):
            proposal = agent.propose(ctx, finding)
        self.assertEqual(proposal.required_auth, ('strong_auth',))
        self.assertIn('report_card_loss', [s['tool'] for s in proposal.suggested_steps])

    def test_missing_card_or_unknown_operation_never_writes(self):
        for entities in ({}, {'card_id':'missing','card_operation':'report_loss'}, {'card_id':'card_001','card_operation':'password_change'}):
            ctx, agent = self.agent('card', self.rt.bank.cards_query(), self.ctx(entities=entities))
            self.assertEqual(agent.propose(ctx, agent.analyze(ctx)).suggested_steps, ())

    def test_unknown_and_already_lost_status_no_repeat_loss(self):
        ctx = self.ctx(entities={'card_id':'card_001','card_operation':'report_loss'})
        for status in ('unknown', 'reported_loss'):
            result = ba.ToolResult(True, 'list_cards', {'items':[{'id':'card_001','status':status}]})
            _, agent = self.agent('card', result, ctx)
            self.assertEqual(agent.propose(ctx, agent.analyze(ctx)).suggested_steps, ())
