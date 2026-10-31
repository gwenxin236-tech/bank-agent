from dataclasses import replace
from datetime import datetime, timezone, timedelta
import unittest
from domain_agent_registry import DOMAIN_AGENT_REGISTRY
from domain_snapshots import family_snapshot
from test_domain_snapshots import SnapshotFixture


class FamilyAgentTests(SnapshotFixture, unittest.TestCase):
    def test_grant_facts_expiry_revocation_and_actor_subject_binding(self):
        store = self.rt.bank.guardianships
        row = store.db.execute('SELECT grant_id FROM guardianships LIMIT 1').fetchone()
        grant = store.get(row[0])
        ctx = self.ctx(actor_id=grant.guardian_id, subject_id=grant.subject_id)
        snapshot = family_snapshot(ctx, grant, datetime.now(timezone.utc))
        agent = DOMAIN_AGENT_REGISTRY.create('family', snapshot)
        facts = {f['name']:f['value'] for f in agent.analyze(ctx).facts}
        self.assertEqual(facts['grant']['daily_limit_cents'], grant.daily_limit_cents)
        self.assertEqual(agent.propose(ctx, agent.analyze(ctx)).suggested_steps, ())
        revoked = DOMAIN_AGENT_REGISTRY.create('family', family_snapshot(ctx, replace(grant,revoked=True), datetime.now(timezone.utc)))
        self.assertIn('grant_inactive', revoked.analyze(ctx).risk_signals)
        with self.assertRaises(PermissionError):
            family_snapshot(self.ctx(), grant, datetime.now(timezone.utc))

    def test_relationship_names_never_authorize_transfer(self):
        ctx, agent = self.agent('family', self.rt.bank.list_family_relationships())
        out = agent.propose(ctx, agent.analyze(ctx))
        self.assertEqual(out.suggested_steps, ())
        self.assertNotIn('authorized', str(out.to_dict()))

    def test_expired_or_absent_grant_cannot_propose_operations(self):
        store = self.rt.bank.guardianships
        grant = store.get(store.db.execute('SELECT grant_id FROM guardianships LIMIT 1').fetchone()[0])
        ctx = self.ctx(actor_id=grant.guardian_id, subject_id=grant.subject_id)
        late = datetime.fromisoformat(grant.expires_at) + timedelta(seconds=1)
        agent = DOMAIN_AGENT_REGISTRY.create('family', family_snapshot(ctx, grant, late))
        self.assertIn('grant_inactive', agent.analyze(ctx).risk_signals)
        self.assertEqual(agent.propose(ctx, agent.analyze(ctx)).suggested_steps, ())
        missing = DOMAIN_AGENT_REGISTRY.create('family', family_snapshot(ctx, None, late))
        self.assertIn('grant_id', missing.analyze(ctx).missing_fields)

