import unittest
import banking_agent as ba
from unittest.mock import patch
from test_domain_snapshots import SnapshotFixture


class FinanceHealthAgentTests(SnapshotFixture, unittest.TestCase):
    def test_two_ledger_views_and_coverage_reuse_existing_facts(self):
        result = self.rt.bank.financial_health_report()
        ctx, agent = self.agent('finance_health', result)
        finding = agent.analyze(ctx)
        facts = {f['name']:f['value'] for f in finding.facts}
        self.assertEqual(facts['consumer']['total_cents'], result.data['consumer_view']['total_cents'])
        self.assertEqual(facts['cash_flow']['outflow_cents'], result.data['cash_flow_view']['outflow_cents'])
        self.assertFalse(facts['coverage']['complete'])
        self.assertIn('coverage_incomplete', finding.risk_signals)

    def test_no_purchase_budget_mutation_or_extra_core_reads(self):
        ctx, agent = self.agent('finance_health', self.rt.bank.financial_health_report())
        before = (self.rt.bank.db.total_changes, self.rt.audit.db.total_changes)
        with patch.object(self.rt.bank, 'financial_health_report', side_effect=AssertionError('read')):
            proposal = agent.propose(ctx, agent.analyze(ctx))
        self.assertEqual(proposal.suggested_steps, ())
        self.assertEqual(before, (self.rt.bank.db.total_changes,self.rt.audit.db.total_changes))

    def test_missing_ledger_totals_not_filled_with_zero(self):
        ctx, agent = self.agent('finance_health', ba.ToolResult(True, 'financial_health_report', {}))
        finding = agent.analyze(ctx)
        self.assertEqual(finding.facts, ())
        self.assertIn('verified_finance_health_facts', finding.missing_fields)

