import unittest
from unittest.mock import patch
from domain_agent_registry import DOMAIN_AGENT_REGISTRY
from domain_snapshots import policy_snapshot
from test_domain_snapshots import SnapshotFixture


class AntifraudAgentTests(SnapshotFixture, unittest.TestCase):
    def test_existing_risk_features_and_no_permission_decision(self):
        ctx = self.ctx(entities={'amount_cents':200000,'beneficiary':'妈妈'})
        decision = self.rt.policy.classify('transfer', dict(ctx.entities))
        agent = DOMAIN_AGENT_REGISTRY.create('antifraud', policy_snapshot(ctx, decision))
        finding = agent.analyze(ctx)
        facts = {f['name']:f['value'] for f in finding.facts}
        self.assertEqual(facts['risk_score'], decision['risk_score'])
        self.assertNotIn('defer', facts)
        self.assertNotIn('risk_level', facts)
        with patch.object(self.rt.bank, 'create_cooling_off', side_effect=AssertionError('write')):
            proposal = agent.propose(ctx, finding)
        self.assertEqual(proposal.suggested_steps, ())

    def test_keyword_signal_reports_factor_not_fraud_verdict(self):
        ctx = self.ctx()
        decision = {'risk_score':10,'risk_factors':[{'code':'scam_phrase','points':10,'reason':'话术信号'}], 'aml_suspect':False}
        agent = DOMAIN_AGENT_REGISTRY.create('antifraud', policy_snapshot(ctx, decision))
        self.assertIn('scam_phrase', agent.analyze(ctx).risk_signals)
        self.assertEqual(self.rt.bank.cooling_off_cases, {})

    def test_missing_source_is_unknown_and_false_permission_flags_not_forwarded(self):
        ctx = self.ctx()
        agent = DOMAIN_AGENT_REGISTRY.create('antifraud', policy_snapshot(ctx, {'authz_allowed':True}))
        finding = agent.analyze(ctx)
        self.assertIn('verified_antifraud_facts', finding.missing_fields)
        self.assertNotIn('authz_allowed', str(finding.to_dict()))
        self.assertEqual(agent.propose(ctx, finding).suggested_steps, ())

