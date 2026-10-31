"""C3: offline credential binding, expiry, single consumption and audit evidence."""
import hashlib
import json
import sys
import tempfile
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import banking_agent as ba
from production_contracts import AdapterContext, MockStrongAuthProvider


class CredentialRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_db = ba.DB_PATH
        ba.DB_PATH = Path(self.tmp.name) / 'c3.sqlite3'
        self.rt = ba.AgentRuntime()

    def tearDown(self):
        self.rt.recovery_scheduler.stop()
        self.rt.bank.close()
        self.rt.audit.db.close()
        ba.DB_PATH = self.old_db
        self.tmp.cleanup()

    def challenge(self):
        out = self.rt.chat('给妈妈转 5000 元')
        plan = self.rt.pending[out['plan']['plan_id']]
        response = self.rt.confirm(plan.plan_id, plan.consent_token)
        self.assertTrue(response['needs_auth'])
        cid = response['auth_challenge']['credential_id']
        return plan, cid

    def passed(self):
        plan, cid = self.challenge()
        self.assertTrue(self.rt.verify(plan.plan_id, plan.consent_token, '123456', credential_id=cid)['ok'])
        return plan, cid

    def events(self, plan):
        return [(row[0], json.loads(row[1])) for row in self.rt.audit.db.execute(
            'SELECT event_type,payload_json FROM audit_events WHERE trace_id=? ORDER BY seq', (plan.trace_id,))]

    def test_all_binding_fields_and_five_minute_ttl_are_auditable(self):
        plan, cid = self.passed()
        cred = self.rt.auth_credentials.get(cid)
        binding = asdict(cred.binding)
        self.assertEqual(binding, dict(user_id='demo-user', device_id='demo-device', task_id=plan.plan_id,
                                      node_id=plan.steps[-1].id, operation_type='create_transfer',
                                      amount_cents=500000, recipient='妈妈'))
        self.assertEqual(cred.expires_at - cred.created_at, timedelta(minutes=5))
        self.assertIsNone(cred.used_at)
        self.assertTrue(cred.nonce)
        out = self.rt.confirm(plan.plan_id, plan.consent_token, credential_id=cid)
        self.assertEqual(out['result']['status'], 'SUCCESS')
        self.assertIsNotNone(self.rt.auth_credentials.get(cid).used_at)
        payload = next(p for event, p in self.events(plan) if event == 'auth.consumed')
        for key in (*binding, 'expires_at', 'nonce_digest', 'used_at', 'operation_digest'):
            self.assertIn(key, payload)
        serialized = json.dumps([p for e, p in self.events(plan) if e.startswith('auth.')], ensure_ascii=False)
        self.assertNotIn(cred.nonce, serialized)
        self.assertNotIn(plan.consent_token, serialized)
        self.assertNotIn('123456', serialized)

    def test_change_recipient_or_amount_invalidates_even_if_restored(self):
        for key, value in [('beneficiary', '爸爸'), ('amount_cents', 400000)]:
            with self.subTest(key=key):
                plan, cid = self.passed()
                old = plan.entities[key]
                plan.entities[key] = value
                rejected = self.rt.confirm(plan.plan_id, plan.consent_token, credential_id=cid)
                self.assertFalse(rejected['ok'])
                self.assertEqual(self.rt.auth_credentials.get(cid).invalidated_reason, 'binding_changed')
                plan.entities[key] = old
                self.assertFalse(self.rt.confirm(plan.plan_id, plan.consent_token, credential_id=cid)['ok'])
                self.assertIsNone(self.rt.auth_credentials.get(cid).used_at)
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(), 0)
        # Legitimate edit path is a new confirmation card and a fresh authentication.
        fresh = self.rt.chat('给妈妈转 4000 元')
        pid, token = fresh['plan']['plan_id'], fresh['plan']['consent_token']
        self.assertTrue(self.rt.confirm(pid, token)['needs_auth'])
        self.assertTrue(self.rt.verify(pid, token, '123456')['ok'])
        self.assertEqual(self.rt.confirm(pid, token)['result']['status'], 'SUCCESS')

    def test_replay_after_pending_removed_is_rejected_and_audited(self):
        plan, cid = self.passed()
        self.assertEqual(self.rt.confirm(plan.plan_id, plan.consent_token)['result']['status'], 'SUCCESS')
        for action in (lambda: self.rt.confirm(plan.plan_id, plan.consent_token, credential_id=cid),
                       lambda: self.rt.verify(plan.plan_id, plan.consent_token, '123456', credential_id=cid)):
            self.assertEqual(action()['error_code'], 'AUTH_REPLAY')
        self.assertEqual(sum(e == 'auth.replay' for e, p in self.events(plan)), 2)
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(), 500000)

    def test_expired_credential_rejected_and_explicit_reauthentication_works(self):
        plan, cid = self.passed()
        clock = self.rt.auth_credentials.get(cid).expires_at
        self.rt.auth_credentials.clock = lambda: clock
        self.assertEqual(self.rt.confirm(plan.plan_id, plan.consent_token, credential_id=cid)['error_code'], 'AUTH_EXPIRED')
        new = self.rt.confirm(plan.plan_id, plan.consent_token)['auth_challenge']['credential_id']
        self.assertNotEqual(new, cid)
        self.assertFalse(self.rt.verify(plan.plan_id, plan.consent_token, '123456', credential_id=cid)['ok'])
        self.assertTrue(self.rt.verify(plan.plan_id, plan.consent_token, '123456', credential_id=new)['ok'])
        self.assertEqual(self.rt.confirm(plan.plan_id, plan.consent_token, credential_id=new)['result']['status'], 'SUCCESS')

    def test_three_wrong_codes_lock_task_even_after_global_breaker_reset(self):
        plan, cid = self.challenge()
        for _ in range(3):
            self.assertFalse(self.rt.verify(plan.plan_id, plan.consent_token, '000000')['ok'])
        self.assertTrue(self.rt.breaker.is_locked())
        self.rt.breaker.reset()
        self.assertEqual(self.rt.verify(plan.plan_id, plan.consent_token, '123456')['error_code'], 'AUTH_LOCKED')
        self.assertEqual(self.rt.confirm(plan.plan_id, plan.consent_token)['error_code'], 'AUTH_LOCKED')
        self.assertEqual(self.rt.auth_credentials.get(cid).attempts, 3)

    def test_double_confirm_consumes_once(self):
        plan, cid = self.passed()
        barrier = threading.Barrier(2)
        def submit():
            barrier.wait()
            return self.rt.confirm(plan.plan_id, plan.consent_token, credential_id=cid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: submit(), range(2)))
        self.assertEqual(sum(r.get('result', {}).get('status') == 'SUCCESS' for r in results), 1)
        self.assertEqual(sum(r.get('error_code') == 'AUTH_REPLAY' for r in results), 1)
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(), 500000)

    def test_direct_execute_cannot_use_verified_but_unconsumed_credential(self):
        plan, cid = self.passed()
        self.assertEqual(self.rt.execute(plan)['status'], 'FAILED')
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(), 0)

    def test_every_bound_context_field_is_checked(self):
        for field, value in [('actor_id', 'other'), ('device_id', 'other-device'), ('plan_id', 'other-task'),
                             ('node_id', 'other-node'), ('operation_type', 'cancel_subscription')]:
            with self.subTest(field=field):
                plan, cid = self.passed()
                original_id = plan.plan_id
                if field == 'node_id':
                    plan.steps[-1].id = value
                elif field == 'operation_type':
                    plan.steps[-1].tool = value
                else:
                    setattr(plan, field, value)
                self.assertFalse(self.rt.confirm(original_id, plan.consent_token, credential_id=cid)['ok'])
                self.assertEqual(self.rt.auth_credentials.get(cid).invalidated_reason, 'binding_changed')

    def test_wrong_request_identity_or_device_and_foreign_credential_rejected(self):
        plan, cid = self.passed()
        for context in [dict(actor_id='intruder'), dict(device_id='stolen-device')]:
            self.assertFalse(self.rt.confirm(plan.plan_id, plan.consent_token, credential_id=cid, **context)['ok'])
        other, other_cid = self.passed()
        self.assertFalse(self.rt.confirm(other.plan_id, other.consent_token, credential_id=cid)['ok'])
        self.assertIsNone(self.rt.auth_credentials.get(other_cid).used_at)


class CredentialAdapterTests(unittest.TestCase):
    def test_adapter_rejects_unissued_changed_context_and_replay(self):
        provider = MockStrongAuthProvider()
        ctx = AdapterContext('trace', 'user', 'device')
        self.assertFalse(provider.verify(ctx, 'fabricated', 'demo-assertion', 'digest').ok)
        cid = provider.begin(ctx, 'digest', 'demo').data['challenge_id']
        self.assertFalse(provider.verify(replace(ctx, device_id='other'), cid, 'demo-assertion', 'digest').ok)
        cid = provider.begin(ctx, 'digest', 'demo').data['challenge_id']
        self.assertTrue(provider.verify(ctx, cid, 'demo-assertion', 'digest').ok)
        self.assertEqual(provider.verify(ctx, cid, 'demo-assertion', 'digest').code, 'AUTH_REPLAY')


if __name__ == '__main__':
    unittest.main()
