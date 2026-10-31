"""D1 HTTP/Agent regressions; offline identities are server-side fixtures, not IAM."""
import copy
from dataclasses import asdict
import http.client
import threading
from http.server import ThreadingHTTPServer
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import banking_agent as ba


class D1SecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = ba.AgentRuntime(db_path=Path(self.tmp.name) / 'd1.sqlite3', offline=True)
        # Only trusted server setup may provision identities. No HTTP registration endpoint.
        self.rt.family.demo_sessions = {
            'demo-user-session': {'user_id': 'demo-user', 'device_id': 'demo-device', 'role': 'user'},
            'alice-session': {'user_id': 'alice', 'device_id': 'alice-device', 'role': 'user'},
            'bob-session': {'user_id': 'bob', 'device_id': 'bob-device', 'role': 'user'},
            'admin-session': {'user_id': 'ops', 'device_id': 'ops-device', 'role': 'admin'},
        }

    def tearDown(self):
        self.rt.recovery_scheduler.stop()
        self.rt.bank.close()
        self.rt.audit.db.close()
        self.tmp.cleanup()

    def http(self, method, path, body=None, session=None):
        # Real loopback HTTP covers headers, JSON parsing, routing and redaction.
        class QuietHandler(ba.Handler):
            def log_message(self, *args):
                pass

        with patch.object(ba, 'runtime', self.rt), patch.object(ba, 'gateway', ba.AgentGateway(self.rt)), \
             patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            with ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as server:
                worker = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
                worker.start()
                connection = http.client.HTTPConnection(*server.server_address, timeout=5)
                try:
                    headers = {'Authorization': 'Bearer ' + session} if session else {}
                    headers['Content-Type'] = 'application/json'
                    encoded = json.dumps(body or {}, ensure_ascii=False).encode('utf-8') if method == 'POST' else None
                    connection.request(method, path, body=encoded, headers=headers)
                    response = connection.getresponse()
                    self.assertIn('application/json', response.getheader('Content-Type', ''))
                    return response.status, json.loads(response.read())
                finally:
                    connection.close()
                    server.shutdown()
                    worker.join(timeout=5)

    def test_subscription_query_http_requires_session_and_bound_confirmation(self):
        with patch.object(self.rt.bank, 'subscriptions_query', wraps=self.rt.bank.subscriptions_query) as query:
            status, rejected = self.http('POST', '/api/chat', {'message': '查询自动续费订阅'})
            self.assertEqual(status, 403)
            query.assert_not_called()
            status, planned = self.http('POST', '/api/chat', {'message': '查询自动续费订阅'}, 'demo-user-session')
            self.assertEqual(status, 200)
            self.assertEqual(planned['plan_view']['risk_level'], 'yellow')
            self.assertNotIn('result', planned)
            query.assert_not_called()
            confirmation = {'plan_id': planned['plan']['plan_id'], 'action_token': planned['plan_view']['action_token']}
            _, rejected = self.http('POST', '/api/confirm', confirmation, 'alice-session')
            self.assertFalse(rejected['ok'])
            query.assert_not_called()
            status, completed = self.http('POST', '/api/confirm', confirmation, 'demo-user-session')
            self.assertEqual(status, 200)
            self.assertEqual(completed['result']['status'], 'SUCCESS')
            query.assert_called_once()

    def test_conversation_context_http_isolated_and_requires_fresh_confirmation(self):
        def chat(text, conversation='context-a', session='demo-user-session'):
            status, out = self.http('POST', '/api/chat', {'message': text, 'conversation_id': conversation}, session)
            self.assertEqual(status, 200)
            return out
        chat('给妈妈转账')
        self.assertNotEqual(chat('200元', conversation='context-b')['plan']['intent'], 'transfer')
        self.assertNotEqual(chat('200元', session='alice-session')['plan']['intent'], 'transfer')
        final = chat('200元')
        self.assertEqual(final['plan']['entities']['beneficiary'], '妈妈')
        self.assertEqual(final['plan']['entities']['amount_cents'], 20000)
        self.assertNotIn('result', final)
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)
        _, result = self.http('POST', '/api/confirm', {'plan_id': final['plan']['plan_id'],
            'action_token': final['plan_view']['action_token']}, 'demo-user-session')
        self.assertEqual(result['result']['status'], 'SUCCESS')

    def test_conversation_context_rejects_forged_identity_before_touching_draft(self):
        body = {'message': '给妈妈转账', 'conversation_id': 'context-a'}
        self.http('POST', '/api/chat', body, 'demo-user-session')
        status, rejected = self.http('POST', '/api/chat', dict(body, message='200元', user_id='demo-user'), 'alice-session')
        self.assertEqual(status, 403)
        self.assertFalse(rejected['ok'])
        _, final = self.http('POST', '/api/chat', dict(body, message='200元'), 'demo-user-session')
        self.assertTrue(final['plan_view']['requires_confirmation'])
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)

    def protected_state(self):
        # Audit is intentionally excluded: a denial MUST append evidence, but may
        # not change task, credential, ledger, subscriptions or recovery state.
        return copy.deepcopy({
            'balance': self.rt.bank.balance().data,
            'transactions': [dict(r) for r in self.rt.bank.db.execute('SELECT * FROM core_transactions ORDER BY transaction_id')],
            'subscriptions': self.rt.bank.subscriptions,
            'pending': {k: asdict(v) for k, v in self.rt.pending.items()},
            'credentials': {k: asdict(v) for k, v in self.rt.auth_credentials._items.items()},
            'authenticated': self.rt.authenticated,
            'authenticated_credential': self.rt.authenticated_credential,
            'consumed_tokens': self.rt.consumed_tokens,
            'executing': self.rt.executing,
            'checkpoints': self.rt.recovery_store.rows(),
        })

    def plan(self, text='给妈妈转 100 元', user='alice'):
        out = self.rt.chat(text, actor_id=user, device_id=user + '-device')
        p = out['plan']
        return p, {'plan_id': p['plan_id'], 'action_token': p['consent_token']}

    def test_missing_subscription_id_is_rejected_without_any_cancellation(self):
        before = copy.deepcopy(self.rt.bank.subscriptions)
        with patch.object(self.rt.bank, 'cancel_subscription', wraps=self.rt.bank.cancel_subscription) as cancel:
            balance = self.rt.bank.balance().data
            status, out = self.http('POST', '/api/chat', {'message': '取消订阅'}, 'demo-user-session')
            self.assertEqual(status, 200)
            self.assertFalse(out['ok'])
            self.assertTrue(out['blocked'])
            self.assertEqual(out['result']['status'], 'FAILED')
            self.assertIn('请说明要取消哪个订阅', out['message'])
            self.assertEqual(self.rt.bank.subscriptions, before)
            self.assertEqual(self.rt.bank.balance().data, balance)
            self.assertEqual(self.rt.bank.db.execute('SELECT count(*) FROM core_transactions').fetchone()[0], 0)
            self.assertNotIn(out['plan']['plan_id'], self.rt.pending)
            self.assertEqual(self.rt.bank.idempotency_results, {})
            cancel.assert_not_called()
        events = self.rt.audit.list_for_trace(out['trace_id'])
        self.assertTrue(any(e['event_type'] == 'tool.denied' and e['payload'].get('reason') == 'SUBSCRIPTION_TARGET_REQUIRED' for e in events))
        self.assertTrue(any(e['event_type'] == 'tool.failed' and e['payload'].get('error_code') == 'SUBSCRIPTION_TARGET_REQUIRED' for e in events))
        self.assertTrue(self.rt.audit.verify_trace(out['trace_id'])['valid'])

    def test_missing_target_is_also_rejected_at_execution_boundary(self):
        plan = self.rt.planner.build('取消订阅')
        with patch.object(self.rt.bank, 'cancel_subscription') as cancel:
            result = self.rt._execute_step(plan, plan.steps[-1])
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'SUBSCRIPTION_TARGET_REQUIRED')
        cancel.assert_not_called()

    def test_keyword_target_is_only_suggestion_until_visible_card_confirmed(self):
        intent, entities = self.rt.parser.parse('取消视频会员订阅')
        self.assertEqual(intent, 'subscription_cancel')
        self.assertIsNone(entities.get('subscription_id'))
        self.assertEqual(entities.get('suggested_subscription_id'), 'sub_001')
        out = self.rt.chat('取消视频会员订阅')
        view = json.dumps(out['plan_view'], ensure_ascii=False)
        self.assertIn('某视频会员', view)
        self.assertIn('25.00', view)
        self.assertIn('申请停止', view)
        self.assertIn('确认后核验办理条件', view)
        self.assertIn('某视频会员', out['plan_view']['plain_summary'])
        self.assertIn('25.00', ' '.join(out['plan_view']['details']))
        self.assertTrue(out['plan_view']['requires_confirmation'])
        self.assertTrue(all(s['status'] == 'active' for s in self.rt.bank.subscriptions))
        plan = self.rt.pending[out['plan']['plan_id']]
        with patch.object(self.rt.bank, 'cancel_subscription', wraps=self.rt.bank.cancel_subscription) as cancel:
            direct = self.rt._execute_step(plan, plan.steps[-1])
            self.assertFalse(direct.ok)
            cancel.assert_not_called()
        result = self.rt.confirm(plan.plan_id, plan.consent_token)
        self.assertEqual(result['result']['status'], 'SUCCESS')
        self.assertEqual([s['status'] for s in self.rt.bank.subscriptions], ['cancelled', 'active'])

    def test_ambiguous_keywords_do_not_choose_first_subscription(self):
        _, entities = self.rt.parser.parse('取消视频或者云盘订阅')
        self.assertFalse(entities.get('subscription_id') or entities.get('suggested_subscription_id'))

    def test_llm_suggestion_uses_core_name_and_price_not_model_display_fields(self):
        with patch.object(self.rt.parser.llm, 'parse', return_value=('subscription_cancel', {
            'subscription_id': 'sub_002', 'subscription_name': '伪造免费服务', 'subscription_amount_cents': 0,
        })):
            out = self.rt.chat('帮我关掉那个自动续费')
        view = json.dumps(out['plan_view'], ensure_ascii=False)
        self.assertIn('云盘服务', view)
        self.assertIn('18.00', view)
        self.assertNotIn('伪造免费服务', view)
        self.assertTrue(out['plan_view']['requires_confirmation'])
        self.assertTrue(all(s['status'] == 'active' for s in self.rt.bank.subscriptions))

    def test_changed_subscription_cannot_reuse_confirmation(self):
        out = self.rt.chat('取消视频会员订阅')
        plan = self.rt.pending[out['plan']['plan_id']]
        plan.entities['subscription_id'] = 'sub_002'
        result = self.rt.confirm(plan.plan_id, plan.consent_token)
        self.assertFalse(result['ok'])
        self.assertTrue(all(s['status'] == 'active' for s in self.rt.bank.subscriptions))

    def test_private_reads_require_session_including_audit_aliases(self):
        p, _ = self.plan()
        paths = ['/api/state', '/api/audit', '/api/v1/audit/events?trace_id=' + p['trace_id'],
                 '/api/v1/audit/verify?trace_id=' + p['trace_id']]
        before = self.protected_state()
        for path in paths:
            for session in (None, 'not-issued'):
                with self.subTest(path=path, session=session):
                    audit_count = self.rt.audit.db.execute('SELECT count(*) FROM audit_events').fetchone()[0]
                    status, out = self.http('GET', path, session=session)
                    self.assertIn(status, (401, 403))
                    self.assertEqual(out['error_code'], 'AUTHZ_DENIED')
                    self.assertFalse(out['ok'])
                    self.assertTrue(out['blocked'])
                    self.assertTrue({'balance', 'items', 'events', 'pending', 'subscriptions', 'plan', 'consent_token'}.isdisjoint(out))
                    self.assertNotIn(p['consent_token'], json.dumps(out))
                    self.assertEqual(self.protected_state(), before)
                    events = self.rt.audit.list_for_trace(out['trace_id'])
                    self.assertEqual(len(events), 1)
                    self.assertEqual(events[0]['event_type'], 'authz.denied')
                    self.assertEqual(events[0]['actor'], 'anonymous')
                    self.assertTrue(events[0]['decision_reason'])
                    self.assertEqual(self.rt.audit.db.execute('SELECT count(*) FROM audit_events').fetchone()[0], audit_count + 1)
                    self.assertTrue(self.rt.audit.verify_trace(out['trace_id'])['valid'])
        self.assertEqual(self.http('GET', '/healthz')[0], 200)

    def test_chat_cannot_impersonate_user_and_preserves_session_device(self):
        self.assertIn(self.http('POST', '/api/chat', {'message': '查询余额', 'user_id': 'alice'})[0], (401, 403))
        status, out = self.http('POST', '/api/chat', {'message': '给妈妈转 100 元'}, 'alice-session')
        self.assertEqual(status, 200)
        self.assertEqual(out['plan']['actor_id'], 'alice')
        self.assertEqual(out['plan']['device_id'], 'alice-device')
        self.assertEqual(self.http('POST', '/api/chat', {'message': '查询余额', 'user_id': 'bob'}, 'alice-session')[0], 403)

    def test_cross_user_confirm_verify_cancel_rejected_and_audited(self):
        p, body = self.plan('给妈妈转 5000 元')
        challenge = self.rt.confirm(**dict(plan_id=body['plan_id'], action_token=body['action_token']))
        body.update(credential_id=challenge['auth_challenge']['credential_id'], code='123456')
        before = self.protected_state()
        for path in ('/api/confirm', '/api/verify', '/api/cancel'):
            for session, extra in [(None, {}), ('bob-session', {}), ('bob-session', {'user_id': 'alice', 'device_id': 'alice-device'}), ('alice-session', {'device_id': 'stolen'})]:
                with self.subTest(path=path, session=session, extra=extra):
                    event_count = len(self.rt.audit.list_for_trace(p['trace_id']))
                    status, out = self.http('POST', path, dict(body, **extra), session)
                    self.assertIn(status, (401, 403))
                    self.assertEqual(out['error_code'], 'AUTHZ_DENIED')
                    self.assertIn(p['plan_id'], self.rt.pending)
                    self.assertFalse(out['ok'])
                    self.assertTrue(out['blocked'])
                    self.assertEqual(self.protected_state(), before)
                    events = self.rt.audit.list_for_trace(p['trace_id'])
                    self.assertEqual(len(events), event_count + 1)
                    self.assertEqual(events[-1]['event_type'], 'authz.denied')
                    self.assertEqual(events[-1]['actor'], 'anonymous' if session is None else session.removesuffix('-session'))
                    self.assertEqual(events[-1]['subject_id'], 'alice')
                    self.assertTrue(events[-1]['decision_reason'])
                    self.assertTrue(self.rt.audit.verify_trace(p['trace_id'])['valid'])
        status, verified = self.http('POST', '/api/verify', body, 'alice-session')
        self.assertEqual(status, 200)
        self.assertTrue(verified['ok'])
        status, done = self.http('POST', '/api/confirm', body, 'alice-session')
        self.assertEqual(status, 200)
        self.assertEqual(done['result']['status'], 'SUCCESS')
        self.assertEqual(self.rt.bank.balance().data['available_cents'], before['balance']['available_cents'] - 500000)
        rows = list(self.rt.bank.db.execute('SELECT status, debit_applied FROM core_transactions'))
        self.assertEqual([tuple(r) for r in rows], [('succeeded', 1)])
        completed = self.protected_state()
        self.assertEqual(self.http('POST', '/api/cancel', body, 'bob-session')[0], 403)
        self.assertEqual(self.protected_state(), completed)

    def test_owner_can_cancel_without_trusting_body_identity(self):
        p, body = self.plan()
        status, out = self.http('POST', '/api/cancel', body, 'alice-session')
        self.assertEqual(status, 200)
        self.assertTrue(out['ok'])
        self.assertNotIn(p['plan_id'], self.rt.pending)

    def test_audit_reads_are_owner_scoped(self):
        p, _ = self.plan()
        other, _ = self.plan(user='bob')
        status, out = self.http('GET', '/api/audit', session='alice-session')
        self.assertEqual(status, 200)
        self.assertTrue(out['items'])
        self.assertNotIn(other['trace_id'], {e['trace_id'] for e in out['items']})
        self.assertEqual(self.http('GET', '/api/v1/audit/events?trace_id=' + p['trace_id'], session='bob-session')[0], 403)
        self.assertEqual(self.http('GET', '/api/v1/audit/events?trace_id=' + p['trace_id'], session='alice-session')[0], 200)
        self.assertEqual(self.http('GET', '/api/state', session='demo-user-session')[0], 200)

    def test_completed_and_pending_task_ownership_survives_restart(self):
        completed, first = self.plan()
        self.assertEqual(self.http('POST', '/api/confirm', first, 'alice-session')[1]['result']['status'], 'SUCCESS')
        pending, second = self.plan()
        sessions = self.rt.family.demo_sessions
        self.rt.bank.close()
        self.rt.audit.db.close()
        self.rt = ba.AgentRuntime(db_path=Path(self.tmp.name) / 'd1.sqlite3', offline=True)
        self.rt.family.demo_sessions = sessions
        for body in (first, second):
            for path in ('/api/confirm', '/api/verify', '/api/cancel'):
                self.assertEqual(self.http('POST', path, body, 'bob-session')[0], 403)
        self.assertIn(pending['plan_id'], self.rt.pending)
        self.assertEqual(self.http('POST', '/api/cancel', second, 'alice-session')[0], 200)
        self.assertEqual(self.rt.bank.db.execute('SELECT count(*) FROM core_transactions').fetchone()[0], 1)
        audit = json.dumps(self.rt.audit.list(), ensure_ascii=False)
        for secret in ('alice-session', 'bob-session', first['action_token'], second['action_token']):
            self.assertNotIn(secret, audit)

    def test_default_session_has_no_operator_privilege_and_cannot_enter_family(self):
        self.assertEqual(self.http('GET', '/api/v1/family/grants', session='demo-user-session')[0], 403)
        self.assertEqual(self.http('POST', '/api/v1/family/chat', {'message': '查询余额', 'subject_id': 'family-father-demo'}, 'demo-user-session')[0], 403)
        self.assertEqual(self.http('POST', '/api/reset-demo', {'role': 'admin'}, 'demo-user-session')[0], 403)
        from family_access import FamilyAccess
        with patch.dict('os.environ', {'BANK_AGENT_DEMO_ADMIN_TOKEN': ''}):
            access = FamilyAccess(self.rt)
        self.assertEqual({x['role'] for x in access.demo_sessions.values()}, {'user'})

    def test_management_requires_explicit_operator_session(self):
        self.rt.frozen_users.add('alice')
        self.rt.breaker.failure(); self.rt.breaker.failure(); self.rt.breaker.failure()
        for session in (None, 'alice-session', 'demo-user-session'):
            self.assertIn(self.http('POST', '/api/v1/audit/unlock', {'target_user_id': 'alice'}, session)[0], (401, 403))
            self.assertIn(self.http('POST', '/api/reset-demo', {}, session)[0], (401, 403))
            self.assertIn('alice', self.rt.frozen_users)
            self.assertTrue(self.rt.breaker.is_locked())
        self.assertEqual(self.http('POST', '/api/v1/audit/unlock', {'target_user_id': 'alice'}, 'admin-session')[0], 200)
        self.assertNotIn('alice', self.rt.frozen_users)
        self.assertEqual(self.http('POST', '/api/reset-demo', {}, 'admin-session')[0], 200)
        self.assertFalse(self.rt.breaker.is_locked())


if __name__ == '__main__':
    unittest.main()
