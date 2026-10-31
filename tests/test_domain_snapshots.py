from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import banking_agent as ba
from domain_agent_contract import DomainContext
from domain_agent_registry import DOMAIN_AGENT_REGISTRY
from domain_snapshots import tool_snapshot, policy_snapshot, family_snapshot


class SnapshotFixture:
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = ba.AgentRuntime(db_path=Path(self.tmp.name) / 'domain.sqlite3', offline=True)

    def tearDown(self):
        self.rt.recovery_scheduler.stop()
        self.rt.bank.close(); self.rt.audit.db.close(); self.tmp.cleanup()

    def ctx(self, **changes):
        base = DomainContext('trace', 'session', 'demo-user', 'demo-user', 'demo-device', 'web', '领域请求', {})
        return replace(base, **changes)

    def agent(self, domain, result, ctx=None):
        ctx = ctx or self.ctx()
        return ctx, DOMAIN_AGENT_REGISTRY.create(domain, tool_snapshot(ctx, domain, result))


class DomainSnapshotTests(SnapshotFixture, unittest.TestCase):
    def test_failed_read_unknown_not_zero_success(self):
        ctx, agent = self.agent('subscription', ba.ToolResult(False, 'list_subscriptions'))
        finding = agent.analyze(ctx)
        self.assertTrue(finding.missing_fields)
        self.assertEqual(agent.propose(ctx, finding).suggested_steps, ())

    def test_wrong_tool_cross_domain_and_global_owner_rejected(self):
        with self.assertRaises(ValueError):
            tool_snapshot(self.ctx(), 'card', self.rt.bank.subscriptions_query())
        with self.assertRaises(PermissionError):
            tool_snapshot(self.ctx(subject_id='alice', actor_id='alice'), 'subscription', self.rt.bank.subscriptions_query())

    def test_snapshot_deep_copy_context_isolation_and_float_rejection(self):
        ctx = self.ctx()
        result = self.rt.bank.subscriptions_query()
        snap = tool_snapshot(ctx, 'subscription', result)
        result.data['items'].clear()
        agent = DOMAIN_AGENT_REGISTRY.create('subscription', snap)
        self.assertTrue(agent.analyze(ctx).facts)
        for key in ('trace_id', 'actor_id', 'subject_id', 'session_id', 'device_id', 'channel'):
            with self.assertRaises(ValueError):
                agent.analyze(replace(ctx, **{key:'different'}))
        bad = ba.ToolResult(True, 'list_subscriptions', {'items':[{'id':'bad','amount_cents':1.5}]})
        with self.assertRaises(ValueError):
            tool_snapshot(ctx, 'subscription', bad)

    def test_main_subscription_only_after_confirmation_and_advisory_failure_does_not_requery(self):
        with patch.object(self.rt.bank, 'subscriptions_query', wraps=self.rt.bank.subscriptions_query) as query:
            out = self.rt.chat('查询自动续费订阅')
            query.assert_not_called()
            self.assertNotIn('domain.subscription.analyzed', [e['event_type'] for e in self.rt.audit.list_for_trace(out['trace_id'])])
            done = self.rt.confirm(out['plan']['plan_id'], out['plan_view']['action_token'])
            self.assertEqual(done['result']['status'], 'SUCCESS')
            self.assertEqual(query.call_count, 1)
            self.assertIn('domain.subscription.analyzed', [e['event_type'] for e in self.rt.audit.list_for_trace(out['trace_id'])])

    def test_agent_failure_preserves_real_read_result_without_retry(self):
        out = self.rt.chat('查询自动续费订阅')
        with patch.object(self.rt.domain_agents, 'create', side_effect=ValueError('private error')), patch.object(self.rt.bank, 'subscriptions_query', wraps=self.rt.bank.subscriptions_query) as query:
            done = self.rt.confirm(out['plan']['plan_id'], out['plan_view']['action_token'])
        self.assertEqual(done['result']['status'], 'SUCCESS')
        self.assertEqual(query.call_count, 1)
        self.assertNotIn('private error', str(done))

    def test_main_domains_have_real_audit_evidence(self):
        for text, domain in [('消费体检','finance_health'),('家庭成员','family'),('给妈妈转1元','antifraud')]:
            out = self.rt.chat(text)
            events = self.rt.audit.list_for_trace(out['trace_id'])
            self.assertIn('domain.' + domain + '.analyzed', [e['event_type'] for e in events])
            self.assertTrue(self.rt.audit.verify_trace(out['trace_id'])['valid'])
        card = self.rt.chat('我的卡丢了，帮我挂失')
        p, token = card['plan']['plan_id'], card['plan_view']['action_token']
        self.rt.confirm(p, token); self.rt.verify(p, token, '123456'); self.rt.confirm(p, token)
        self.assertIn('domain.card.analyzed', [e['event_type'] for e in self.rt.audit.list_for_trace(card['trace_id'])])

    def test_all_five_domains_are_pure_and_have_no_live_core_handles(self):
        ctx = self.ctx()
        sources = [('finance_health',self.rt.bank.financial_health_report()), ('family',self.rt.bank.list_family_relationships()),
                   ('subscription',self.rt.bank.subscriptions_query()), ('card',self.rt.bank.cards_query())]
        agents = [self.agent(domain,result,ctx)[1] for domain,result in sources]
        agents.append(DOMAIN_AGENT_REGISTRY.create('antifraud', policy_snapshot(ctx, {'risk_score':0,'risk_factors':[]})))
        before = (self.rt.bank.db.total_changes, self.rt.audit.db.total_changes)
        for agent in agents:
            with self.subTest(domain=agent.domain), patch.object(self.rt.bank, 'transfer', side_effect=AssertionError('write')), \
                 patch.object(self.rt.bank, 'subscriptions_query', side_effect=AssertionError('read')), \
                 patch.object(self.rt.audit, 'write', side_effect=AssertionError('audit')):
                finding = agent.analyze(ctx)
                agent.propose(ctx, finding)
                self.assertFalse(hasattr(agent,'bank'))
                self.assertFalse(hasattr(agent,'runtime'))
        self.assertEqual(before, (self.rt.bank.db.total_changes, self.rt.audit.db.total_changes))

    def test_global_tools_not_relabelled_as_another_subject(self):
        for domain,result in [('finance_health',self.rt.bank.financial_health_report()), ('family',self.rt.bank.list_family_relationships()),
                              ('subscription',self.rt.bank.subscriptions_query()), ('card',self.rt.bank.cards_query())]:
            with self.subTest(domain=domain), self.assertRaises(PermissionError):
                tool_snapshot(self.ctx(actor_id='alice',subject_id='alice'),domain,result)

    def test_concurrent_factory_instances_never_share_context_or_selection(self):
        result = self.rt.bank.subscriptions_query()
        def run(number):
            sid = 'sub_001' if number % 2 == 0 else 'sub_002'
            ctx = self.ctx(trace_id='trace-' + str(number), session_id='session-' + str(number),
                           entities={'subscription_id':sid, 'operation':'cancel'})
            _, agent = self.agent('subscription', result, ctx)
            proposal = agent.propose(ctx, agent.analyze(ctx))
            return sid, proposal.to_dict()['suggested_steps']
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(run, range(12)))
        for sid, steps in results:
            self.assertEqual([s['params']['subscription_id'] for s in steps], ['sub_001'] if sid == 'sub_001' else [])
        self.assertTrue(all(item['status']=='active' for item in self.rt.bank.subscriptions))


if __name__ == '__main__':
    unittest.main()
