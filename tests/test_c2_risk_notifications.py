import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import banking_agent as ba
from risk_controls import score_transaction_risk


class RiskNotificationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / 'risk.sqlite3'
        self.rt = ba.AgentRuntime(db_path=self.db, offline=True)

    def tearDown(self):
        self.rt.bank.close()
        self.rt.audit.db.close()
        self.tmp.cleanup()

    def seed(self, amount=20000, actor='demo-user', name='妈妈', count=1):
        for i in range(count):
            result = self.rt.bank.transfer(amount, name, '', 'seed-' + ba.new_id('tx'), actor)
            self.assertEqual(result.data['status'], 'succeeded')

    def events(self, trace):
        return self.rt.audit.list_for_trace(trace)

    def test_score_components_and_integer_boundary(self):
        facts = dict(amount_cents=100000, is_new_recipient=True, keyword_hit=True,
                     is_elderly=True, recent_transfer_count=2)
        result = score_transaction_risk(**facts)
        self.assertEqual(result['risk_score'], 150)
        self.assertEqual(sorted(x['points'] for x in result['risk_factors']), [20, 20, 30, 30, 50])
        self.assertEqual(score_transaction_risk(99999, False, False, False, 0)['risk_score'], 0)
        self.assertEqual(score_transaction_risk(100000, False, False, False, 0)['risk_score'], 20)
        for amount in (True, 1000.0, -1):
            with self.assertRaises(ValueError):
                score_transaction_risk(amount, False, False, False, 0)

    def test_score_60_defers_without_debit_and_notifies_guardian(self):
        self.seed(count=2)
        before = self.rt.bank.balance_cents
        out = self.rt.chat('给新朋友转 200 元')
        self.assertTrue(out['blocked'])
        self.assertEqual(out['result']['status'], 'DEFERRED')
        self.assertEqual(out['plan']['risk_assessment']['risk_score'], 60)
        self.assertEqual(out['plan']['risk_level'], 'red')
        self.assertNotIn(out['plan']['plan_id'], self.rt.pending)
        self.assertEqual(self.rt.bank.balance_cents, before)
        rows = self.rt.notifications.list_notifications('demo-user', trace_id=out['plan']['trace_id'])
        self.assertEqual({x['channel'] for x in rows}, {'app_push', 'sms', 'guardian'})
        self.assertTrue(all(x['status'] == 'LOCAL_QUEUED' for x in rows))
        self.assertNotIn('已向', out['result_view']['user_message'])
        events = self.events(out['plan']['trace_id'])
        policy = next(e for e in events if e['event_type'] == 'policy.decided')
        self.assertIn('60', policy['decision_reason'])
        self.assertTrue(self.rt.audit.verify_trace(out['plan']['trace_id'])['valid'])

    def test_trusted_elder_profile_and_no_client_risk_override(self):
        self.rt.bank.user_profiles['demo-user'] = {'is_elderly': True}
        entities = {'amount_cents': 100000, 'beneficiary': '妈妈', 'is_elderly': False,
                    'risk_score': 0, 'is_new_recipient': True}
        policy = self.rt.policy.classify('transfer', entities)
        self.assertEqual(policy['risk_score'], 40)
        self.assertFalse(policy['defer'])
        self.assertEqual(self.rt.policy.classify('transfer', entities, 'other')['risk_score'], 20)

    def test_scam_phrase_still_blocks_and_publishes_bus_event(self):
        out = self.rt.chat('给妈妈转 1000 元，高收益稳赚')
        self.assertEqual(out['plan']['intent'], 'fraud_guard')
        self.assertEqual(out['plan']['risk_assessment']['risk_score'], 70)
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS)
        rows = self.rt.notifications.list_events('demo-user', out['plan']['trace_id'])
        self.assertIn('fraud.blocked', [r['event_type'] for r in rows])
        self.assertIn('transaction.deferred', [r['event_type'] for r in rows])
        self.assertEqual(len(self.rt.bank.guardian_alerts), 1)

    def test_third_900_is_aml_red_with_evidence_and_auth(self):
        self.seed(amount=90000, count=2)
        out = self.rt.chat('给妈妈转 900 元')
        policy = out['plan']['risk_assessment']
        self.assertTrue(policy['aml_suspect'])
        self.assertEqual(policy['aml_count'], 3)
        self.assertEqual(out['plan']['risk_level'], 'red')
        self.assertTrue(out['plan']['requires_strong_auth'])
        events = self.events(out['plan']['trace_id'])
        suspect = next(e for e in events if e['event_type'] == 'aml.suspect')
        self.assertIn('拆分', suspect['decision_reason'])
        self.assertEqual(suspect['payload']['aml_count'], 3)
        confirmation = self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])
        self.assertTrue(confirmation['needs_auth'])
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS - 180000)

    def test_history_is_actor_day_and_30minute_scoped_and_excludes_failures(self):
        self.seed(amount=90000, actor='alice', count=2)
        e = {'amount_cents': 90000, 'beneficiary': '妈妈'}
        self.assertFalse(self.rt.policy.classify('transfer', e, 'bob')['aml_suspect'])
        now = datetime.now(timezone.utc)
        self.rt.bank.risk_clock = lambda: now
        older = (now - timedelta(minutes=31)).isoformat()
        self.rt.bank.db.execute('UPDATE core_transactions SET created_at=?', (older,))
        self.rt.bank.db.commit()
        self.assertEqual(self.rt.policy.classify('transfer', e, 'alice')['recent_transfer_count'], 0)
        yesterday = (now - timedelta(days=1)).isoformat()
        self.rt.bank.db.execute('UPDATE core_transactions SET created_at=?', (yesterday,))
        self.rt.bank.db.commit()
        self.assertFalse(self.rt.policy.classify('transfer', e, 'alice')['aml_suspect'])
        self.rt.bank.inject_fault('insufficient_funds')
        for i in range(3):
            self.rt.bank.transfer(90000, '妈妈', '', 'failed-' + str(i), 'bob')
        self.assertFalse(self.rt.policy.classify('transfer', e, 'bob')['aml_suspect'])

    def test_planning_repeatedly_does_not_fake_third_transfer(self):
        for _ in range(4):
            out = self.rt.chat('给妈妈转 900 元')
            self.assertFalse(out['plan']['risk_assessment']['aml_suspect'])
            self.assertEqual(out['plan']['risk_assessment']['recent_transfer_count'], 0)

    def test_confirm_rechecks_risk_after_plan_created(self):
        # Initially verified; directory change plus new transfers raises risk at confirm.
        self.rt.bank.beneficiaries['新朋友'] = '13700137000'
        out = self.rt.chat('给新朋友转 200 元')
        self.assertIn(out['plan']['plan_id'], self.rt.pending)
        del self.rt.bank.beneficiaries['新朋友']
        self.seed(count=2)
        before = self.rt.bank.balance_cents
        held = self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])
        self.assertEqual(held['result']['status'], 'DEFERRED')
        self.assertEqual(self.rt.bank.balance_cents, before)

    def test_otp_failure_and_device_mismatch_publish_with_no_otp(self):
        out = self.rt.chat('给妈妈转 5000 元')
        pid, token = out['plan']['plan_id'], out['plan']['consent_token']
        self.rt.confirm(pid, token)
        self.rt.verify(pid, token, '654321')
        self.rt.confirm(pid, token, device_id='untrusted-device')
        events = self.rt.notifications.list_events('demo-user', out['plan']['trace_id'])
        self.assertIn('auth.failed', [e['event_type'] for e in events])
        self.assertIn('device.risk', [e['event_type'] for e in events])
        notices = self.rt.notifications.list_notifications('demo-user')
        audit = self.events(out['plan']['trace_id'])
        encoded = json.dumps([notices, events, audit])
        self.assertNotIn('654321', encoded)
        self.assertNotIn('123456', encoded)
        for notice in notices:
            self.assertTrue(any(e['payload'].get('notification_id') == notice['notification_id'] and e['event_type'] == 'notification.queued' for e in audit))

    def test_bus_idempotency_subscriptions_and_restart_queries(self):
        self.rt.notifications.subscribe('alice', 'app_push', 'alice-app', ['device.risk'])
        self.rt.notifications.publish('device.risk', 'alice', 'trace-c2', {'reason': 'test'}, 'same-event')
        self.rt.notifications.publish('device.risk', 'alice', 'trace-c2', {'reason': 'test'}, 'same-event')
        rows = self.rt.notifications.list_notifications('alice')
        self.assertEqual(len([x for x in rows if x['target'] == 'alice-app']), 1)
        self.assertEqual(self.rt.notifications.list_notifications('bob'), [])
        self.rt.bank.close(); self.rt.audit.db.close()
        self.rt = ba.AgentRuntime(db_path=self.db, offline=True)
        self.assertEqual(self.rt.notifications.list_notifications('alice'), rows)
        self.assertEqual(len(self.rt.notifications.list_events('alice')), 1)

    def test_guardian_revocation_prevents_delivery(self):
        for auth in self.rt.bank.family_authorizations:
            auth['revoked'] = True
        self.seed(count=2)
        out = self.rt.chat('给新朋友转 200 元')
        self.assertTrue(out['blocked'])
        rows = self.rt.notifications.list_notifications('demo-user')
        self.assertFalse(any(x['channel'] == 'guardian' and x['status'] == 'LOCAL_QUEUED' for x in rows))

    def test_processing_publishes_deferred_without_changing_core_truth(self):
        self.rt.bank.inject_fault('timeout')
        out = self.rt.chat('给妈妈转 200 元')
        result = self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])
        self.assertEqual(result['result']['status'], 'PROCESSING')
        self.assertIn('银行还在确认', result['message'])
        self.assertIn('transaction.deferred', [r['event_type'] for r in self.rt.notifications.list_events('demo-user')])

    def test_three_900_end_to_end_red_and_audited(self):
        for i in range(3):
            out = self.rt.chat('给妈妈转 900 元')
            plan = out['plan']
            result = self.rt.confirm(plan['plan_id'], plan['consent_token'])
            if i:
                self.assertTrue(result['needs_auth'])
                verified = self.rt.verify(plan['plan_id'], plan['consent_token'], '123456')
                result = self.rt.confirm(plan['plan_id'], plan['consent_token'], credential_id=verified['credential_id'])
            self.assertEqual(result['result']['status'], 'SUCCESS')
            self.assertEqual(plan['risk_assessment']['aml_suspect'], i == 2)
            if i == 2:
                self.assertEqual(plan['risk_level'], 'red')
                self.assertEqual(plan['risk_assessment']['risk_score'], 30)
                self.assertIn('aml.suspect', [e['event_type'] for e in self.events(plan['trace_id'])])
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS - 270000)

    def test_aml_amount_and_window_boundaries(self):
        self.seed(amount=90000, count=2)
        now = datetime.now(timezone.utc)
        self.rt.bank.risk_clock = lambda: now
        self.rt.bank.db.execute('UPDATE core_transactions SET created_at=?', ((now - timedelta(minutes=30)).isoformat(),))
        self.rt.bank.db.commit()
        for amount, hit in ((89999, False), (90000, True), (99999, True), (100000, False)):
            with self.subTest(amount=amount):
                decision = self.rt.policy.classify('transfer', {'amount_cents': amount, 'beneficiary': '妈妈'})
                self.assertEqual(decision['aml_suspect'], hit)
                self.assertEqual(decision['recent_transfer_count'], 2)
        self.rt.bank.risk_clock = lambda: now + timedelta(microseconds=1)
        self.assertEqual(self.rt.policy.classify('transfer', {'amount_cents': 90000, 'beneficiary': '妈妈'})['recent_transfer_count'], 0)

    def test_persistent_history_survives_restart(self):
        self.seed(amount=90000, count=2)
        self.rt.bank.close(); self.rt.audit.db.close()
        self.rt = ba.AgentRuntime(db_path=self.db, offline=True)
        policy = self.rt.chat('给妈妈转 900 元')['plan']['risk_assessment']
        self.assertTrue(policy['aml_suspect'])
        self.assertEqual(policy['recent_transfer_count'], 2)

    def test_risk_rechecked_at_side_effect_after_confirmation(self):
        out = self.rt.chat('给李明转 200 元')
        self.rt.bank.user_profiles['demo-user']['is_elderly'] = True
        original = self.rt.bank.resolve_beneficiary
        def changed(beneficiary):
            result = original(beneficiary)
            self.seed(count=2)
            self.rt.bank.beneficiaries.pop('李明')
            return result
        with patch.object(self.rt.bank, 'resolve_beneficiary', side_effect=changed):
            held = self.rt.confirm(out['plan']['plan_id'], out['plan']['consent_token'])
        self.assertEqual(held['result']['status'], 'DEFERRED')
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS - 40000)
        self.assertEqual(len(self.rt.notifications.list_events('demo-user')), 1)

    def test_three_otp_failures_remain_locked_and_notified(self):
        out = self.rt.chat('给妈妈转 5000 元')['plan']
        self.rt.confirm(out['plan_id'], out['consent_token'])
        for _ in range(3):
            self.rt.verify(out['plan_id'], out['consent_token'], '654321')
        result = self.rt.verify(out['plan_id'], out['consent_token'], '123456')
        self.assertTrue(result.get('blocked') or not result['ok'])
        failures = [e for e in self.rt.notifications.list_events('demo-user') if e['event_type'] == 'auth.failed']
        self.assertGreaterEqual(len(failures), 3)
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS)

    def test_guardian_expired_wrong_subject_and_wrong_actor_rejected(self):
        grant = next(g for g in self.rt.bank.family_authorizations if g['action'] == 'receive_fraud_alert')
        for field, value in [('expires_at', '2000-01-01T00:00:00+00:00'), ('subject', '陌生人'), ('actor', 'stranger')]:
            with self.subTest(field=field), patch.dict(grant, {field: value}):
                self.assertIsNone(self.rt.bank.guardian_target('demo-user'))
                with self.assertRaises(ValueError):
                    self.rt.notifications.subscribe('demo-user', 'guardian', 'family-child-demo')

    def test_revocation_after_subscription_records_suppression(self):
        bus = self.rt.notifications
        bus.publish('device.risk', 'demo-user', 'trace-first', {'reason': 'first'}, 'first')
        for g in self.rt.bank.family_authorizations:
            g['revoked'] = True
        bus.publish('device.risk', 'demo-user', 'trace-second', {'reason': 'second'}, 'second')
        guardian = bus.list_notifications('demo-user', 'trace-second', 'guardian')
        self.assertEqual([g['status'] for g in guardian], ['SUPPRESSED'])
        self.assertIn('notification.suppressed', [e['event_type'] for e in self.events('trace-second')])

    def test_bus_restart_completes_partial_dispatch_without_duplicate_queue(self):
        write = self.rt.audit.write
        def fail_notice(trace, event_type, payload, **kwargs):
            if event_type == 'notification.queued':
                raise RuntimeError('injected audit unavailable')
            return write(trace, event_type, payload, **kwargs)
        with patch.object(self.rt.audit, 'write', side_effect=fail_notice):
            with self.assertRaises(RuntimeError):
                self.rt.notifications.publish('device.risk', 'demo-user', 'trace-recover', {'reason': 'recovery'}, 'recover')
        self.assertEqual(self.rt.notifications.list_notifications('demo-user'), [])
        self.rt.bank.close(); self.rt.audit.db.close()
        self.rt = ba.AgentRuntime(db_path=self.db, offline=True)
        self.rt.notifications.recover_pending()
        notices = self.rt.notifications.list_notifications('demo-user', 'trace-recover')
        self.assertEqual(len(notices), 3)
        self.assertEqual(len({n['notification_id'] for n in notices}), 3)
        self.assertTrue(self.rt.audit.verify_trace('trace-recover')['valid'])

    def test_notification_ids_survive_sanitizer_and_payload_is_masked(self):
        import uuid
        # UUID 中出现长数字串不能破坏通知与审计的关联，也不能为修 ID 关闭脱敏。
        with patch('notification_bus.uuid.uuid4', side_effect=[uuid.UUID(int=int('12345678123456781234567812345678', 16) + i) for i in range(20)]):
            self.rt.notifications.publish('device.risk', 'demo-user', 'trace-mask',
                {'phone': '13812345678', 'card': '6222021234567890123',
                 'identity': '110101199001011234', 'otp': '654321'}, 'mask')
        rows = self.rt.notifications.list_notifications('demo-user')
        audit = self.events('trace-mask')
        for notice in rows:
            self.assertEqual(ba.sanitize_audit_value(notice['notification_id']), notice['notification_id'])
            self.assertTrue(any(e['event_type'] == 'notification.queued' and
                                e['payload']['notification_id'] == notice['notification_id'] and
                                e['payload']['event_id'] == notice['event_id'] for e in audit))
        encoded = json.dumps([rows, audit])
        for secret in ('13812345678', '6222021234567890123', '110101199001011234', '654321'):
            self.assertNotIn(secret, encoded)

    def test_concurrent_confirmations_cannot_cross_risk_window(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        self.seed(count=1)
        self.rt.bank.user_profiles['demo-user']['is_elderly'] = True
        plans = [self.rt.chat('给妈妈转 1000 元')['plan'] for _ in range(2)]
        for plan in plans:
            self.rt.confirm(plan['plan_id'], plan['consent_token'])
            self.rt.verify(plan['plan_id'], plan['consent_token'], '123456')
        barrier = threading.Barrier(2)
        original = self.rt.bank.resolve_beneficiary
        def resolve(name):
            result = original(name)
            barrier.wait(timeout=5)
            return result
        with patch.object(self.rt.bank, 'resolve_beneficiary', side_effect=resolve), ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(self.rt.confirm, p['plan_id'], p['consent_token']) for p in plans]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(sorted(r['result']['status'] for r in results), ['DEFERRED', 'SUCCESS'])
        self.assertEqual(self.rt.bank.balance_cents, self.rt.bank.INITIAL_BALANCE_CENTS - 120000)

    def test_recover_unaudited_guardian_notice_rechecks_revocation(self):
        write = self.rt.audit.write
        def fail_guardian(trace, event_type, payload, **kwargs):
            if event_type == 'notification.queued' and payload.get('channel') == 'guardian':
                raise RuntimeError('injected audit unavailable')
            return write(trace, event_type, payload, **kwargs)
        with patch.object(self.rt.audit, 'write', side_effect=fail_guardian):
            with self.assertRaises(RuntimeError):
                self.rt.notifications.publish('device.risk', 'demo-user', 'trace-revoke', {}, 'revoke')
        for grant in self.rt.bank.family_authorizations:
            grant['revoked'] = True
        self.rt.notifications.recover_pending()
        rows = self.rt.notifications.list_notifications('demo-user', 'trace-revoke', 'guardian')
        self.assertEqual([r['status'] for r in rows], ['SUPPRESSED'])
        self.assertIn('notification.suppressed', [e['event_type'] for e in self.events('trace-revoke')])

    def test_event_key_conflict_and_cross_user_guardian_receipt_are_rejected(self):
        bus = self.rt.notifications
        bus.publish('device.risk', 'demo-user', 'trace-key', {}, 'key')
        with self.assertRaises(ValueError):
            bus.publish('auth.failed', 'demo-user', 'trace-key', {}, 'key')
        receipt = bus.list_notifications('demo-user', 'trace-key', 'guardian')[0]
        receipt.update(user_id='other-user', event_type='fraud.blocked')
        result = self.rt.bank.notify_guardian('case', '妈妈', 10000, 'bad-receipt', notification=receipt)
        self.assertFalse(result.ok)
        self.assertEqual(self.rt.bank.guardian_alerts, [])

    def test_notification_http_query_requires_user_and_filters(self):
        self.rt.notifications.publish('device.risk', 'alice', 'trace-api', {'reason': 'demo'}, 'api-event')
        handler = object.__new__(ba.Handler)
        handler._send = lambda status, body, *args: (status, body)
        with patch.object(ba, 'runtime', self.rt):
            handler.path = '/api/v1/notifications'
            self.assertEqual(handler.do_GET()[0], 400)
            handler.path = '/api/v1/notifications?user_id=alice&trace_id=trace-api&channel=app_push'
            status, data = handler.do_GET()
            self.assertEqual(status, 200)
            self.assertTrue(data['items'])
            self.assertTrue(all(x['user_id'] == 'alice' and x['channel'] == 'app_push' for x in data['items']))
            handler.path = '/api/v1/notifications/events?user_id=bob'
            self.assertEqual(handler.do_GET()[1]['items'], [])


if __name__ == '__main__':
    unittest.main()
