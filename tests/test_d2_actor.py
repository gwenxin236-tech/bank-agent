"""Request actor and account subject must not be conflated in audit evidence."""
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import banking_agent as ba
from channel_contracts import AgentGateway, UnifiedMessage

FATHER, CHILD = 'family-father-demo', 'family-daughter-demo'
FD, CD = 'father-device', 'daughter-device'


class ActorEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'actor.db'
        self.rt = ba.AgentRuntime(db_path=self.path, offline=True)
        self.instances = [self.rt]

    def tearDown(self):
        for rt in self.instances:
            rt.recovery_scheduler.stop()
            rt.bank.close()
            rt.audit.db.close()
        self.tmp.cleanup()

    def events(self, trace, kind=None):
        return [e for e in self.rt.audit.list_for_trace(trace) if kind is None or e['event_type'] == kind]

    def assert_identity(self, events, actor, subject, device):
        self.assertTrue(events)
        for e in events:
            self.assertEqual(e['actor'], actor, e['event_type'])
            self.assertEqual(e['user_id'], subject)
            self.assertEqual(e['subject_id'], subject)
            self.assertEqual(e['device_id'], device)
            self.assertEqual(e['payload']['_identity'], dict(actor_id=actor, subject_id=subject, device_id=device))

    def test_guardian_query_keeps_subject_but_records_actual_caller(self):
        out = self.rt.family.chat('查询余额', CHILD, CD, FATHER)
        self.assert_identity(self.events(out['trace_id']), CHILD, FATHER, CD)
        self.assertEqual(out['result']['results'][0]['data']['account_owner_id'], FATHER)
        self.assertTrue(self.rt.audit.verify_trace(out['trace_id'])['valid'])

    def test_owner_authentication_then_child_confirmation_after_restart(self):
        out = self.rt.family.chat('给妈妈转 200 元', CHILD, CD, FATHER)
        p = out['plan']; args = (p['plan_id'], p['consent_token'])
        challenge = self.rt.confirm(*args, actor_id=CHILD, device_id=CD)
        cid = challenge['auth_challenge']['credential_id']
        verified = self.rt.verify(*args, '123456', credential_id=cid, actor_id=FATHER, device_id=FD)
        self.assertTrue(verified['ok'])
        self.assert_identity(self.events(out['trace_id'], 'auth.passed'), FATHER, FATHER, FD)
        self.rt = ba.AgentRuntime(db_path=self.path, offline=True); self.instances.append(self.rt)
        done = self.rt.confirm(*args, credential_id=cid, actor_id=CHILD, device_id=CD)
        self.assertEqual(done['result']['status'], 'SUCCESS')
        self.assert_identity(self.events(out['trace_id'], 'consent.granted'), CHILD, FATHER, CD)
        self.assert_identity(self.events(out['trace_id'], 'tool.called'), CHILD, FATHER, CD)
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(FATHER), 20000)
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(CHILD), 0)
        self.assertTrue(self.rt.audit.verify_trace(out['trace_id'])['valid'])

    def test_cross_user_and_anonymous_denials_never_impersonate_subject(self):
        out = self.rt.chat('给妈妈转 200 元')
        body = {'plan_id': out['plan']['plan_id']}
        for action in ('confirm', 'verify', 'cancel'):
            denied = self.rt.family.http_context('Bearer demo-daughter-session', body, required=True, action=action)
            self.assertFalse(denied['ok'])
            self.assert_identity(self.events(out['trace_id'], 'authz.denied')[-1:], CHILD, 'demo-user', CD)
        self.rt.family.http_context('', body, required=True, action='confirm')
        self.assert_identity(self.events(out['trace_id'], 'authz.denied')[-1:], 'anonymous', 'demo-user', 'unknown-device')

    def test_gateway_and_parallel_requests_do_not_leak_identity(self):
        gateway = AgentGateway(self.rt)
        def run(i):
            actor = 'user-' + chr(97+i)
            out = gateway.handle(UnifiedMessage('web', actor, actor, text='查询余额'))
            self.assert_identity(self.events(out['trace_id']), actor, actor, 'demo-device')
        with ThreadPoolExecutor(4) as pool:
            list(pool.map(run, range(8)))
        direct = self.rt.chat('查询余额')
        self.assert_identity(self.events(direct['trace_id']), 'demo-user', 'demo-user', 'demo-device')

    def test_actor_column_tamper_is_detected_and_freezes_subject(self):
        out = self.rt.family.chat('查询余额', CHILD, CD, FATHER)
        e = self.events(out['trace_id'])[0]
        self.rt.audit.db.execute('UPDATE audit_events SET actor=? WHERE id=?', ('intruder', e['id']))
        self.rt.audit.db.commit()
        result = self.rt.verify_audit_trace(out['trace_id'])
        self.assertFalse(result['valid'])
        self.assertEqual(result['break_at'], e['seq'])
        self.assertEqual(result['frozen_user'], FATHER)
        self.assertTrue(self.rt.is_user_frozen(FATHER))
        self.assertFalse(self.rt.is_user_frozen(CHILD))
        self.assert_identity(self.events(out['trace_id'], 'security.tamper'), 'system:audit-verifier', FATHER, 'system')

    def test_guardian_cancel_records_guardian_not_owner(self):
        out = self.rt.family.chat('给妈妈转 200 元', CHILD, CD, FATHER)
        p = out['plan']
        result = self.rt.cancel(p['plan_id'], p['consent_token'], actor_id=CHILD, device_id=CD)
        self.assertTrue(result['ok'])
        self.assert_identity(self.events(out['trace_id'], 'plan.cancelled'), CHILD, FATHER, CD)
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(FATHER), 0)

    def test_exception_resets_context_and_payload_cannot_forge_identity(self):
        from unittest.mock import patch
        with patch.object(self.rt.planner, 'build', side_effect=RuntimeError('test')):
            with self.assertRaises(RuntimeError):
                self.rt.chat('查询余额', actor_id='alice', device_id='alice-device')
        self.assertIsNone(self.rt.audit._identity.get())
        self.rt.audit.write('fresh', 'test', {'user_id':'bob', 'device_id':'bob-device',
            '_identity':{'actor_id':'alice', 'subject_id':'alice', 'device_id':'alice-device'}}, actor='bob')
        self.assert_identity(self.events('fresh'), 'bob', 'bob', 'bob-device')

    def test_legacy_hash_chain_remains_verifiable_without_rewriting_history(self):
        self.rt.audit.write('legacy', 'old.event', {'user_id': 'legacy-owner'}, actor='legacy-owner')
        e = self.events('legacy')[0]
        e['payload'].pop('_identity', None)
        digest = self.rt.audit._hash_event(e)
        self.rt.audit.db.execute('UPDATE audit_events SET payload_json=?, current_hash=?, event_hash=? WHERE id=?',
            (json.dumps(e['payload']), digest, digest, e['id']))
        self.rt.audit.db.commit()
        self.rt.chat('查询余额', actor_id='new-owner')
        self.assertTrue(self.rt.audit.verify_trace('legacy')['valid'])
        latest = self.rt.audit.list(1)[0]
        self.assertTrue(self.rt.audit.verify_trace(latest['trace_id'])['valid'])

    def test_processing_reconciler_records_system_not_the_account_owner(self):
        out = self.rt.chat('给妈妈转 200 元', actor_id='alice', device_id='alice-device')
        p = out['plan']
        self.rt.bank.inject_fault('timeout')
        done = self.rt.confirm(p['plan_id'], p['consent_token'], actor_id='alice', device_id='alice-device')
        transaction = next(x['data']['transaction_id'] for x in done['result']['results'] if x['tool']=='create_transfer')
        self.rt.bank.finalize_transaction(transaction, 'succeeded')
        self.rt.reconcile_recovery_tasks()
        self.assert_identity(self.events(out['trace_id'], 'task.recovery.reconciled'), 'system:recovery', 'alice', 'system')


if __name__ == '__main__':
    unittest.main()
