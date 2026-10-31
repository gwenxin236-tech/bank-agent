"""C4 authorization matrix; all identities here are trusted test adapter contexts."""
import json
from pathlib import Path
import sys
import tempfile
import unittest
from datetime import timedelta
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import banking_agent as ba

FATHER = 'family-father-demo'
DAUGHTER = 'family-daughter-demo'
OWNER_DEVICE = 'father-device'
CHILD_DEVICE = 'daughter-device'


class GuardianshipTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'family.sqlite3'
        self.rt = ba.AgentRuntime(db_path=self.path, offline=True)
        self.instances = [self.rt]

    def tearDown(self):
        for rt in self.instances:
            rt.recovery_scheduler.stop()
            rt.bank.close()
            rt.audit.db.close()
        self.tmp.cleanup()

    def restart(self):
        self.rt = ba.AgentRuntime(db_path=self.path, offline=True)
        self.instances.append(self.rt)
        return self.rt

    def chat(self, text, mode='delegate'):
        return self.rt.family.chat(text, DAUGHTER, CHILD_DEVICE, FATHER, mode)

    def prepare(self, amount=600, mode='delegate'):
        out = self.chat(f'给妈妈转 {amount} 元', mode)
        self.assertTrue(out['ok'], out)
        plan = out['plan']
        return plan['plan_id'], plan['consent_token']

    def confirm(self, plan, cid='', owner=False):
        return self.rt.confirm(*plan, credential_id=cid,
            actor_id=FATHER if owner else DAUGHTER, device_id=OWNER_DEVICE if owner else CHILD_DEVICE)

    def authenticate(self, plan, owner_confirm=False):
        challenge = self.confirm(plan, owner=owner_confirm)
        self.assertTrue(challenge.get('needs_auth'), challenge)
        cid = challenge['auth_challenge']['credential_id']
        self.assertEqual(challenge['auth_challenge']['binding']['user_id'], FATHER)
        out = self.rt.verify(*plan, '123456', credential_id=cid, actor_id=FATHER, device_id=OWNER_DEVICE)
        self.assertTrue(out['ok'], out)
        return cid

    def events(self, trace):
        return self.rt.audit.list_for_trace(trace)

    def remaining(self):
        return self.rt.bank.guardianships.grants_for(DAUGHTER)[0]['remaining_cents']

    def test_daughter_reads_fathers_balance_and_bills_not_her_account(self):
        out = self.chat('查询余额')
        self.assertEqual(out['result']['status'], 'SUCCESS')
        result = out['result']['results'][0]['data']
        self.assertEqual(result['account_owner_id'], FATHER)
        self.assertEqual(result['available_cents'], 2000000)
        self.assertNotEqual(result['available_cents'], self.rt.bank.balance(DAUGHTER).data['available_cents'])
        for text in ['查询交易流水', '账单分析']:
            out = self.chat(text)
            data = out['result']['results'][0]['data']
            self.assertEqual(data['account_owner_id'], FATHER)
            self.assertNotIn('星巴克', json.dumps(data, ensure_ascii=False))

    def test_small_transfer_requires_owner_auth_then_debits_owner_and_quota_once(self):
        plan = self.prepare(1000)
        initial_demo = self.rt.bank.balance_cents
        initial_child = self.rt.bank.balance(DAUGHTER).data['available_cents']
        challenge = self.confirm(plan)
        self.assertTrue(challenge['needs_auth'])
        cid = challenge['auth_challenge']['credential_id']
        denied = self.rt.verify(*plan, '123456', cid, CHILD_DEVICE, DAUGHTER)
        self.assertEqual(denied['error_code'], 'AUTHZ_DENIED')
        self.assertEqual(self.remaining(), 100000)
        self.assertTrue(self.rt.verify(*plan, '123456', cid, OWNER_DEVICE, FATHER)['ok'])
        self.assertEqual(self.confirm(plan, cid)['result']['status'], 'SUCCESS')
        self.assertEqual(self.remaining(), 0)
        self.assertEqual(self.rt.bank.balance(FATHER).data['available_cents'], 1900000)
        self.assertEqual(self.rt.bank.balance_cents, initial_demo)
        self.assertEqual(self.rt.bank.balance(DAUGHTER).data['available_cents'], initial_child)
        self.assertFalse(self.confirm(plan, cid)['ok'])
        self.restart()
        self.assertEqual(self.remaining(), 0)
        self.assertEqual(self.rt.bank.balance(FATHER).data['available_cents'], 1900000)
        self.assertFalse(self.confirm(plan, cid)['ok'])

    def test_large_delegation_denied_with_audit_owner_assistance_can_complete(self):
        out = self.chat('给妈妈转 5000 元')
        self.assertEqual(out['error_code'], 'AUTHZ_DENIED')
        self.assertTrue(any(e['event_type'] == 'authz.denied' for e in self.events(out['trace_id'])))
        plan = self.prepare(5000, 'assist')
        self.assertEqual(self.confirm(plan)['error_code'], 'AUTHZ_DENIED')
        cid = self.authenticate(plan, owner_confirm=True)
        self.assertEqual(self.confirm(plan, cid, owner=True)['result']['status'], 'SUCCESS')
        self.assertEqual(self.remaining(), 100000)  # owner transaction is not a delegation
        self.assertEqual(self.rt.bank.balance(FATHER).data['available_cents'], 1500000)

    def test_expiry_and_revocation_rechecked_after_owner_auth(self):
        for field in ['expires_at', 'revoked']:
            with self.subTest(field=field):
                grant = self.rt.bank.guardianships.grants_for(DAUGHTER)[0]
                self.rt.bank.guardianships.update(grant['grant_id'], expires_at='2099-12-31T23:59:59+00:00', revoked=False)
                plan = self.prepare(100)
                cid = self.authenticate(plan)
                value = (self.rt.bank.guardianships.clock() - timedelta(seconds=1)).isoformat() if field == 'expires_at' else True
                self.rt.bank.guardianships.update(grant['grant_id'], **{field: value})
                denied = self.confirm(plan, cid)
                self.assertEqual(denied['error_code'], 'AUTHZ_DENIED')
                self.assertEqual(self.chat('查询余额')['error_code'], 'AUTHZ_DENIED')
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(FATHER), 0)

    def test_read_only_and_full_never_override_hard_personal_only_rules(self):
        grant = self.rt.bank.guardianships.grants_for(DAUGHTER)[0]
        for auth_type in ['read_only', 'full']:
            self.rt.bank.guardianships.update(grant['grant_id'], authorization_type=auth_type)
            self.assertTrue(self.chat('查询余额')['ok'])
            for text in ['给妈妈转 5000 元', '修改密码', '银行卡挂失', '银行卡解挂']:
                self.assertEqual(self.chat(text)['error_code'], 'AUTHZ_DENIED')
            if auth_type == 'read_only':
                self.assertEqual(self.chat('给妈妈转 100 元')['error_code'], 'AUTHZ_DENIED')
            else:
                plan = self.prepare(100)
                self.assertTrue(self.confirm(plan)['needs_auth'])

    def test_concurrent_transfers_cannot_overdraw_daily_delegation_limit(self):
        plans = [self.prepare(600) for _ in range(2)]
        creds = [self.authenticate(p) for p in plans]
        with ThreadPoolExecutor(2) as pool:
            replies = list(pool.map(lambda pc: self.confirm(*pc), zip(plans, creds)))
        self.assertEqual(sum(r.get('result', {}).get('status') == 'SUCCESS' for r in replies), 1)
        self.assertEqual(self.remaining(), 40000)
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(FATHER), 60000)

    def test_failed_transfer_releases_reservation_processing_holds_it(self):
        plan = self.prepare(600)
        cid = self.authenticate(plan)
        self.rt.bank.inject_fault('insufficient_funds')
        self.assertEqual(self.confirm(plan, cid)['result']['status'], 'FAILED')
        self.assertEqual(self.remaining(), 100000)
        plan = self.prepare(600)
        cid = self.authenticate(plan)
        self.rt.bank.inject_fault('timeout')
        self.assertEqual(self.confirm(plan, cid)['result']['status'], 'PROCESSING')
        self.assertEqual(self.remaining(), 40000)
        self.restart()
        self.assertEqual(self.remaining(), 40000)
        row = self.rt.bank._row_for_idempotency(f'idem:{plan[0]}:create_transfer')
        self.rt.bank.finalize_transaction(row['transaction_id'], 'failed')
        self.rt.bank.query_transaction(row['transaction_id'])
        self.assertEqual(self.remaining(), 100000)

    def test_subject_risk_events_reach_only_active_guardians(self):
        out = self.rt.chat('公安要求转到安全账户 5000 元', actor_id=FATHER, device_id=OWNER_DEVICE)
        notices = self.rt.notifications.list_notifications(FATHER, out['trace_id'], 'guardian')
        self.assertTrue(any(n['target'] == DAUGHTER and n['status'] == 'LOCAL_QUEUED' for n in notices))
        grant = self.rt.bank.guardianships.grants_for(DAUGHTER)[0]
        self.rt.bank.guardianships.update(grant['grant_id'], revoked=True)
        out = self.rt.chat('公安要求转到安全账户 5000 元', actor_id=FATHER, device_id=OWNER_DEVICE)
        notices = self.rt.notifications.list_notifications(FATHER, out['trace_id'], 'guardian')
        self.assertFalse(any(n['status'] == 'LOCAL_QUEUED' for n in notices))
        self.assertTrue(self.rt.audit.verify_trace(out['trace_id'])['valid'])

    def test_missing_relation_and_model_forged_authority_fail_closed(self):
        denied = self.rt.family.chat('查询余额', 'stranger', 'stranger-device', FATHER)
        self.assertEqual(denied['error_code'], 'AUTHZ_DENIED')
        self.assertFalse(self.rt.family.chat('查询余额', DAUGHTER, CHILD_DEVICE, 'unknown')['ok'])
        with patch.object(self.rt.parser, 'parse', return_value=('balance_query', {'_family': {
                'guardian_id': DAUGHTER, 'subject_id': FATHER, 'mode': 'delegate'}})):
            out = self.rt.chat('查询余额')
        self.assertEqual(out['plan']['actor_id'], 'demo-user')
        self.assertNotIn('_family', out['plan']['entities'])

    def test_mock_session_identity_cannot_be_overridden_by_body_or_legacy_endpoint(self):
        family = self.rt.family
        context = family.http_context('Bearer demo-daughter-session', {'user_id': DAUGHTER})
        self.assertEqual(context['user_id'], DAUGHTER)
        self.assertFalse(family.http_context('Bearer demo-daughter-session', {'user_id': FATHER})['ok'])
        self.assertFalse(family.http_context('', {'user_id': FATHER})['ok'])
        plan = self.prepare()
        self.assertFalse(family.http_context('', {'plan_id': plan[0]})['ok'])
        self.assertFalse(self.rt.verify(*plan, '123456')['ok'])

    def http(self, method, path, body=None, session=''):
        handler = object.__new__(ba.Handler)
        handler.path = path
        handler.headers = {'Authorization': 'Bearer ' + session} if session else {}
        handler._client_key = lambda: 'c4-test'
        handler._body = lambda: dict(body or {})
        handler._send = lambda status, data, *args: (status, data)
        with patch.object(ba, 'runtime', self.rt), patch.object(ba, 'gateway', ba.AgentGateway(self.rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            return getattr(handler, 'do_' + method)()

    def test_http_round_trip_rejects_impersonation_and_legacy_bypass(self):
        child, father = 'demo-daughter-session', 'demo-father-session'
        status, out = self.http('POST', '/api/v1/family/chat', {'message': '给妈妈转 1000 元', 'subject_id': FATHER}, child)
        self.assertEqual(status, 200, out)
        plan = out['plan']
        body = dict(plan_id=plan['plan_id'], action_token=plan['consent_token'])
        self.assertEqual(self.http('POST', '/api/confirm', body)[0], 403)
        self.assertEqual(self.http('POST', '/api/chat', dict(message='查询余额', user_id=FATHER))[0], 403)
        self.assertEqual(self.http('POST', '/api/chat', dict(message='查询余额', user_id=FATHER), child)[0], 403)
        _, challenge = self.http('POST', '/api/confirm', body, child)
        body['credential_id'] = challenge['auth_challenge']['credential_id']
        self.assertFalse(self.http('POST', '/api/verify', dict(body, code='123456'), child)[1]['ok'])
        self.assertTrue(self.http('POST', '/api/verify', dict(body, code='123456'), father)[1]['ok'])
        _, out = self.http('POST', '/api/confirm', body, child)
        self.assertEqual(out['result']['status'], 'SUCCESS')
        _, grants = self.http('GET', '/api/v1/family/grants', session=child)
        self.assertEqual(grants['items'][0]['remaining_cents'], 0)
        self.assertNotIn('demo_session', json.dumps(grants))

    def test_http_status_audit_notifications_do_not_leak_family_data(self):
        plan = self.prepare()
        cid = self.authenticate(plan)
        out = self.confirm(plan, cid)
        tx = self.rt.bank._row_for_idempotency(f'idem:{plan[0]}:create_transfer')['transaction_id']
        for path in [f'/api/v1/transactions/status?transaction_id={tx}',
                     f'/api/v1/audit/events?trace_id={out["trace_id"]}',
                     f'/api/v1/notifications?user_id={FATHER}']:
            self.assertEqual(self.http('GET', path)[0], 403, path)
            self.assertEqual(self.http('GET', path, session='demo-mother-session')[0], 403, path)
            self.assertEqual(self.http('GET', path, session='demo-father-session')[0], 200, path)
        status, audit = self.http('GET', '/api/audit')
        self.assertEqual(status, 403)
        self.assertEqual(audit['error_code'], 'AUTHZ_DENIED')
        self.assertNotIn(FATHER, json.dumps(audit))
        self.assertNotIn(DAUGHTER, json.dumps(audit))

    def test_pending_owner_auth_recovers_but_expired_credential_does_not(self):
        plan = self.prepare()
        cid = self.authenticate(plan)
        self.restart()
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(FATHER), 0)
        credential = self.rt.auth_credentials.get(cid)
        credential.expires_at = ba.datetime.now(ba.timezone.utc) - timedelta(seconds=1)
        self.assertFalse(self.confirm(plan, cid)['ok'])
        fresh = self.authenticate(plan)
        self.assertNotEqual(fresh, cid)
        self.assertEqual(self.confirm(plan, fresh)['result']['status'], 'SUCCESS')

    def test_personal_sensitive_assistance_only_hands_off_without_fake_success(self):
        for text in ['修改密码', '银行卡挂失', '银行卡解挂']:
            out = self.chat(text, 'assist')
            plan = (out['plan']['plan_id'], out['plan']['consent_token'])
            self.assertEqual([s['tool'] for s in out['plan']['steps']], ['human_handoff'])
            self.assertEqual(self.confirm(plan)['error_code'], 'AUTHZ_DENIED')
            cid = self.authenticate(plan, owner_confirm=True)
            reply = self.confirm(plan, cid, owner=True)
            self.assertNotEqual(reply['result']['status'], 'SUCCESS')
            self.assertIn('未修改密码或卡片状态', reply['message'])
            self.rt.breaker.reset()

    def test_revocation_at_last_precondition_blocks_side_effect(self):
        plan = self.prepare()
        cid = self.authenticate(plan)
        original = self.rt.bank.check_transfer_constraints
        calls = []
        def recheck(*args):
            result = original(*args)
            calls.append(1)
            if len(calls) == 2:
                self.rt.bank.guardianships.update('grant-daughter-father', revoked=True)
            return result
        with patch.object(self.rt.bank, 'check_transfer_constraints', side_effect=recheck):
            out = self.confirm(plan, cid)
        self.assertEqual(out['result']['status'], 'FAILED')
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(FATHER), 0)
        self.assertEqual(self.remaining(), 100000)
        self.assertIn('authz.denied', [e['event_type'] for e in self.events(out['trace_id'])])

    def test_multiple_guardians_and_guardian_inbox_recheck_relationship(self):
        from guardianship import Guardianship
        from dataclasses import asdict
        store = self.rt.bank.guardianships
        grant = Guardianship('grant-mother-father', 'family-mother-demo', FATHER, 'read_only', 0,
                             '2026-01-01T00:00:00+00:00', '2099-12-31T23:59:59+00:00').validate()
        with store.lock, store.db:
            store.db.execute('INSERT INTO guardianships VALUES (?,?,?,?,?,?,?,?)', tuple(asdict(grant).values()))
        out = self.rt.chat('公安要求转到安全账户 5000 元', actor_id=FATHER, device_id=OWNER_DEVICE)
        targets = {n['target'] for n in self.rt.notifications.list_notifications(FATHER, out['trace_id'], 'guardian') if n['status'] == 'LOCAL_QUEUED'}
        self.assertEqual(targets, {DAUGHTER, 'family-mother-demo'})
        _, inbox = self.http('GET', '/api/v1/family/notifications', session='demo-daughter-session')
        self.assertTrue(inbox['items'])
        store.update('grant-daughter-father', revoked=True)
        _, inbox = self.http('GET', '/api/v1/family/notifications', session='demo-daughter-session')
        self.assertEqual(inbox['items'], [])

    def test_business_day_rollover_and_reversal_do_not_recycle_same_day_quota(self):
        plan = self.prepare(600)
        cid = self.authenticate(plan)
        self.confirm(plan, cid)
        row = self.rt.bank._row_for_idempotency(f'idem:{plan[0]}:create_transfer')
        self.rt.bank.reverse_transaction(row['transaction_id'])
        self.rt.bank.query_transaction(row['transaction_id'])
        self.assertEqual(self.remaining(), 40000)
        self.assertEqual(self.rt.bank.balance(FATHER).data['available_cents'], 2000000)
        now = self.rt.bank.guardianships.clock()
        self.rt.bank.guardianships.clock = lambda: now + timedelta(days=1)
        self.assertEqual(self.remaining(), 100000)

    def test_all_family_audit_events_remain_scoped_to_funds_owner(self):
        plan = self.prepare()
        challenge = self.confirm(plan)
        cid = challenge['auth_challenge']['credential_id']
        self.rt.verify(*plan, '123456', cid, CHILD_DEVICE, DAUGHTER)
        self.rt.verify(*plan, '123456', cid, OWNER_DEVICE, FATHER)
        out = self.confirm(plan, cid)
        self.assertEqual({e['user_id'] for e in self.events(out['trace_id'])}, {FATHER})
        self.assertTrue(self.rt.audit.verify_trace(out['trace_id'])['valid'])
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)  # legacy demo endpoint cannot reveal family spend

    def test_family_query_cannot_invoke_a_registered_but_unrelated_side_effect(self):
        plan = self.rt.planner.build('查询余额', actor_id=FATHER, device_id=OWNER_DEVICE)
        result = self.rt._execute_step(plan, ba.PlanStep('forged', '越权取消', 'cancel_subscription'))
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, 'AUTHZ_DENIED')
        self.assertEqual(self.rt.bank.subscriptions[0]['status'], 'active')

    def test_assist_revocation_on_last_balance_check_cannot_execute(self):
        plan = self.prepare(5000, 'assist')
        cid = self.authenticate(plan, owner_confirm=True)
        original = self.rt.bank.check_transfer_constraints
        calls = []
        def recheck(*args):
            result = original(*args)
            calls.append(1)
            if len(calls) == 2:
                self.rt.bank.guardianships.update('grant-daughter-father', revoked=True)
            return result
        with patch.object(self.rt.bank, 'check_transfer_constraints', side_effect=recheck):
            out = self.confirm(plan, cid, owner=True)
        self.assertEqual(out['result']['status'], 'FAILED')
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(FATHER), 0)


if __name__ == '__main__':
    unittest.main()
