import unittest
import banking_agent as ba
from unittest.mock import patch
from test_domain_snapshots import SnapshotFixture


class CardAgentTests(SnapshotFixture, unittest.TestCase):
    def card_case(self, status, operation='report_loss', extra=()):
        result = ba.ToolResult(True, 'list_cards', {'items':[{'id':'card_001','status':status}, *extra]})
        return self.agent('card', result, self.ctx(entities={'card_id':'card_001','card_operation':operation}))

    def test_unloss_has_exact_steps_and_preserves_auth(self):
        ctx, agent = self.card_case('reported_loss', 'unreport_loss')
        proposal = agent.propose(ctx, agent.analyze(ctx))
        self.assertEqual([s['tool'] for s in proposal.suggested_steps], ['list_cards','unreport_card_loss','verify_card_status'])
        self.assertEqual(proposal.required_auth, ('strong_auth',))
        self.assertEqual(proposal.required_confirmations, ('explicit_user_confirmation',))

    def test_state_matrix_explains_noop_and_unknown(self):
        for status, operation, signal, phrase in (
            ('reported_loss','report_loss','card_already_lost','无需重复挂失'),
            ('normal','unreport_loss','card_already_normal','无需解挂'),
            ('unknown','report_loss','card_status_unknown','先核实'),
        ):
            with self.subTest(status=status, operation=operation):
                ctx, agent = self.card_case(status, operation)
                finding = agent.analyze(ctx)
                self.assertIn(signal, finding.risk_signals)
                self.assertIn(phrase, ' '.join(finding.recommendations))
                self.assertEqual(agent.propose(ctx, finding).suggested_steps, ())

    def test_operation_is_explicitly_missing(self):
        ctx, agent = self.card_case('normal', 'password_change')
        self.assertIn('card_operation', agent.analyze(ctx).missing_fields)

    def test_other_card_unknown_does_not_mark_selected_card_unknown(self):
        ctx, agent = self.card_case('normal', extra=[{'id':'other','status':'unknown'}])
        self.assertNotIn('card_status_unknown', agent.analyze(ctx).risk_signals)

    def test_duplicate_target_never_proposes_write(self):
        ctx, agent = self.card_case('normal', extra=[{'id':'card_001','status':'reported_loss'}])
        finding = agent.analyze(ctx)
        self.assertIn('card_identity_ambiguous', finding.risk_signals)
        self.assertEqual(agent.propose(ctx, finding).suggested_steps, ())

    def test_effect_explanation_does_not_promise_payment_or_cancel_subscription(self):
        ctx, agent = self.card_case('reported_loss', 'unreport_loss')
        text = ' '.join(agent.analyze(ctx).recommendations)
        self.assertIn('支付预检', text)
        self.assertIn('订阅', text)
        self.assertIn('隐藏', text)

    def test_failed_read_not_reported_as_no_cards(self):
        ctx, agent = self.agent('card', ba.ToolResult(False, 'list_cards'))
        self.assertIn('未能核实', ' '.join(agent.analyze(ctx).recommendations))
        self.assertEqual(agent.propose(ctx, agent.analyze(ctx)).suggested_steps, ())

    def test_supervisor_and_facts_preserved_for_all_states(self):
        from supervisor import Supervisor
        from domain_agent_registry import DOMAIN_AGENT_REGISTRY
        for status in ('normal','reported_loss','unknown'):
            for operation in ('report_loss','unreport_loss'):
                ctx, agent = self.card_case(status, operation)
                finding = agent.analyze(ctx)
                self.assertEqual(finding.facts, agent.snapshot.facts)
                self.assertEqual(finding.evidence, agent.snapshot.evidence)
                self.assertEqual(finding, agent.analyze(ctx))
                Supervisor(DOMAIN_AGENT_REGISTRY).observe(ctx, 'card', agent.snapshot)

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
