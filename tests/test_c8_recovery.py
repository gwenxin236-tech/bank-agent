"""C8: durable confirmation gates, live readiness and real process crash drill."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import timedelta
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))
import banking_agent as ba


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'recovery.sqlite3'
        self.runtimes = []
        self.rt = self.restart()

    def tearDown(self):
        for rt in self.runtimes:
            if hasattr(rt, 'recovery_scheduler'):
                rt.recovery_scheduler.stop()
            rt.audit.db.close()
            rt.bank.close()
        self.tmp.cleanup()

    def restart(self):
        rt = ba.AgentRuntime(db_path=self.path, offline=True)
        self.runtimes.append(rt)
        return rt

    def plan(self, amount=100):
        out = self.rt.chat(f'给妈妈转 {amount} 元')
        return self.rt.pending[out['plan']['plan_id']]

    def status(self, rt, plan):
        return rt.task_status(plan.plan_id, plan.consent_token, plan.actor_id, plan.device_id)

    def test_waiting_checkpoint_restores_without_executing(self):
        plan = self.plan()
        rt = self.restart()
        self.assertEqual(self.status(rt, plan)['status'], 'waiting_confirmation')
        self.assertEqual(rt.bank.daily_transfer_total_cents(), 0)
        self.assertTrue(all(s.status == 'pending' for s in rt.pending[plan.plan_id].steps))
        self.assertEqual(rt.confirm(plan.plan_id, plan.consent_token)['result']['status'], 'SUCCESS')
        again = self.restart()
        self.assertEqual(self.status(again, plan)['status'], 'succeeded')
        self.assertFalse(again.confirm(plan.plan_id, plan.consent_token)['ok'])
        self.assertEqual(again.bank.daily_transfer_total_cents(), 10000)
        self.assertEqual(again.bank.db.execute('SELECT count(*) FROM core_transactions').fetchone()[0], 1)

    def test_status_requires_token_user_and_device_and_exposes_no_secrets(self):
        plan = self.plan()
        for token, actor, device in [('', plan.actor_id, plan.device_id),
                                     (plan.consent_token, 'other', plan.device_id),
                                     (plan.consent_token, plan.actor_id, 'other')]:
            self.assertFalse(self.rt.task_status(plan.plan_id, token, actor, device)['ok'])
        out = self.status(self.rt, plan)
        self.assertNotIn(plan.consent_token, json.dumps(out))
        self.assertNotIn('entities', out)

    def test_expired_passed_credential_requires_fresh_auth_after_restart(self):
        plan = self.plan(5000)
        cid = self.rt.confirm(plan.plan_id, plan.consent_token)['auth_challenge']['credential_id']
        self.assertTrue(self.rt.verify(plan.plan_id, plan.consent_token, '123456', cid)['ok'])
        self.rt.auth_credentials.get(cid).expires_at -= timedelta(minutes=6)
        self.rt.save_task_checkpoint(plan, 'waiting_confirmation')
        rt = self.restart()
        self.assertEqual(self.status(rt, plan)['status'], 'waiting_confirmation')
        self.assertEqual(rt.confirm(plan.plan_id, plan.consent_token, cid)['error_code'], 'AUTH_EXPIRED')
        fresh = rt.confirm(plan.plan_id, plan.consent_token)['auth_challenge']['credential_id']
        self.assertNotEqual(fresh, cid)
        self.assertEqual(rt.bank.daily_transfer_total_cents(), 0)
        self.assertTrue(rt.verify(plan.plan_id, plan.consent_token, '123456', fresh)['ok'])
        self.assertEqual(rt.confirm(plan.plan_id, plan.consent_token, fresh)['result']['status'], 'SUCCESS')
        again = self.restart()
        self.assertEqual(again.confirm(plan.plan_id, plan.consent_token, fresh)['error_code'], 'AUTH_REPLAY')

    def test_three_wrong_codes_remain_locked_after_restart(self):
        plan = self.plan(5000)
        cid = self.rt.confirm(plan.plan_id, plan.consent_token)['auth_challenge']['credential_id']
        for _ in range(3):
            self.rt.verify(plan.plan_id, plan.consent_token, '000000', cid)
        rt = self.restart()
        self.assertEqual(rt.confirm(plan.plan_id, plan.consent_token)['error_code'], 'AUTH_LOCKED')
        self.assertEqual(rt.verify(plan.plan_id, plan.consent_token, '123456', cid)['error_code'], 'AUTH_LOCKED')

    def test_crash_after_core_commit_reconciles_without_reexecuting(self):
        plan = self.plan()
        execute = self.rt.execute
        def crash(p):
            execute(p)
            raise RuntimeError('simulated process loss after core commit')
        with patch.object(self.rt, 'execute', side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.rt.confirm(plan.plan_id, plan.consent_token)
        rt = self.restart()
        with patch.object(rt, 'execute', side_effect=AssertionError('must not execute')):
            rt.reconcile_recovery_tasks()
            self.assertEqual(self.status(rt, plan)['status'], 'succeeded')
            self.assertFalse(rt.confirm(plan.plan_id, plan.consent_token)['ok'])
        self.assertEqual(rt.bank.daily_transfer_total_cents(), 10000)
        self.assertTrue(rt.verify_audit_trace(plan.trace_id)['valid'])

    def test_crash_before_tool_is_manual_recovery_not_retry(self):
        plan = self.plan()
        with patch.object(self.rt, 'execute', side_effect=RuntimeError('crash before tool')):
            with self.assertRaises(RuntimeError):
                self.rt.confirm(plan.plan_id, plan.consent_token)
        rt = self.restart()
        rt.reconcile_recovery_tasks()
        self.assertEqual(self.status(rt, plan)['status'], 'recovery_required')
        self.assertFalse(rt.confirm(plan.plan_id, plan.consent_token)['ok'])
        self.assertEqual(rt.bank.daily_transfer_total_cents(), 0)

    def test_processing_stays_uncertain_until_core_finalizes_and_tracks_reversal(self):
        plan = self.plan()
        self.rt.bank.inject_fault('timeout')
        self.assertEqual(self.rt.confirm(plan.plan_id, plan.consent_token)['result']['status'], 'PROCESSING')
        rt = self.restart()
        for _ in range(3):
            out = self.status(rt, plan)
            self.assertEqual(out['status'], 'processing')
            self.assertEqual(out['message'], '银行还在确认这笔交易，请不要重复操作')
        row = rt.bank._row_for_idempotency(f'idem:{plan.plan_id}:create_transfer')
        rt.bank.finalize_transaction(row['transaction_id'])
        self.assertEqual(self.status(rt, plan)['status'], 'succeeded')
        rt.bank.reverse_transaction(row['transaction_id'])
        self.assertEqual(self.status(rt, plan)['status'], 'reversed')
        self.assertFalse(rt.confirm(plan.plan_id, plan.consent_token)['ok'])
        self.assertEqual(rt.bank.balance_cents, rt.bank.INITIAL_BALANCE_CENTS)
        self.assertTrue(rt.verify_audit_trace(plan.trace_id)['valid'])

    def test_missing_core_record_cannot_reuse_stale_success(self):
        plan = self.plan()
        self.rt.confirm(plan.plan_id, plan.consent_token)
        with self.rt.bank.db:
            self.rt.bank.db.execute('DELETE FROM core_transactions')
        self.assertEqual(self.status(self.restart(), plan)['status'], 'recovery_required')

    def test_restored_high_risk_task_rechecks_audit_integrity(self):
        plan = self.plan(5000)
        with self.rt.audit.db:
            self.rt.audit.db.execute("UPDATE audit_events SET decision_reason='changed' WHERE trace_id=?",
                                    (plan.trace_id,))
        self.rt.verify_audit_trace(plan.trace_id)
        rt = self.restart()
        self.assertTrue(rt.is_user_frozen(plan.actor_id))
        self.assertFalse(rt.confirm(plan.plan_id, plan.consent_token)['ok'])
        self.assertEqual(rt.bank.daily_transfer_total_cents(), 0)

    def test_failed_core_result_is_not_reported_as_success_after_restart(self):
        plan = self.plan()
        self.rt.bank.inject_fault('insufficient_funds')
        self.assertEqual(self.rt.confirm(plan.plan_id, plan.consent_token)['result']['status'], 'FAILED')
        self.assertEqual(self.status(self.restart(), plan)['status'], 'failed')
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(), 0)

    def test_processing_checkpoint_failure_prevents_tool_call(self):
        plan = self.plan()
        with patch.object(self.rt.recovery_store, 'save', side_effect=sqlite3.OperationalError('locked')), \
                patch.object(self.rt, 'execute') as execute:
            out = self.rt.confirm(plan.plan_id, plan.consent_token)
        self.assertEqual(out['error_code'], 'RECOVERY_UNAVAILABLE')
        execute.assert_not_called()
        self.assertFalse(self.rt.confirm(plan.plan_id, plan.consent_token)['ok'])
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(), 0)

    def test_post_commit_checkpoint_failure_recovers_known_core_result(self):
        plan = self.plan()
        save = self.rt.recovery_store.save
        def fail_final(task_id, status, payload):
            if status == 'succeeded':
                raise sqlite3.OperationalError('injected final checkpoint failure')
            return save(task_id, status, payload)
        with patch.object(self.rt.recovery_store, 'save', side_effect=fail_final):
            out = self.rt.confirm(plan.plan_id, plan.consent_token)
        self.assertEqual(out['error_code'], 'RECOVERY_UNAVAILABLE')
        self.assertNotIn('未发生扣款', out['message'])
        rt = self.restart()
        self.assertEqual(self.status(rt, plan)['status'], 'succeeded')
        self.assertFalse(rt.confirm(plan.plan_id, plan.consent_token)['ok'])
        self.assertEqual(rt.bank.daily_transfer_total_cents(), 10000)

    def test_scheduler_exception_and_stale_heartbeat_are_not_ready(self):
        scheduler = self.rt.recovery_scheduler
        scheduler.callback = lambda: (_ for _ in ()).throw(RuntimeError('simulated scheduler crash'))
        scheduler.start()
        scheduler._thread.join(timeout=2)
        self.assertFalse(self.rt.recovery_readiness()['checks']['scheduler_running'])
        scheduler.callback = lambda: None
        scheduler.interval = 60
        scheduler.start()
        deadline = time.monotonic() + 2
        while not scheduler.running() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(scheduler.running())
        scheduler._heartbeat -= 60
        self.assertFalse(self.rt.recovery_readiness()['checks']['scheduler_running'])

    def test_checkpoint_failure_never_acknowledges_waiting_or_executes(self):
        with patch.object(self.rt.recovery_store, 'save', side_effect=sqlite3.OperationalError('locked')):
            out = self.rt.chat('给妈妈转 100 元')
        self.assertFalse(out['ok'])
        self.assertEqual(out['error_code'], 'RECOVERY_UNAVAILABLE')
        self.assertEqual(self.rt.bank.daily_transfer_total_cents(), 0)

    def test_cancelled_and_expired_tasks_do_not_resurrect(self):
        plan = self.plan()
        self.rt.cancel(plan.plan_id, plan.consent_token)
        self.assertEqual(self.status(self.restart(), plan)['status'], 'cancelled')
        expired = self.plan()
        expired.token_expires_at = (ba.datetime.now(ba.timezone.utc) - timedelta(seconds=1)).isoformat()
        self.rt.save_task_checkpoint(expired, 'waiting_confirmation')
        rt = self.restart()
        self.assertFalse(rt.confirm(expired.plan_id, expired.consent_token)['ok'])
        self.assertNotIn(expired.plan_id, self.restart().pending)

    def test_readiness_checks_real_write_and_live_scheduler(self):
        self.assertFalse(self.rt.recovery_readiness()['ok'])
        self.rt.recovery_scheduler.start()
        deadline = time.monotonic() + 2
        while not self.rt.recovery_readiness()['ok'] and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(self.rt.recovery_readiness()['ok'])
        lock = sqlite3.connect(self.path)
        try:
            lock.execute('BEGIN IMMEDIATE')
            out = self.rt.recovery_readiness()
            self.assertFalse(out['ok'])
            self.assertFalse(out['checks']['database_writable'])
        finally:
            lock.rollback()
            lock.close()
        self.rt.recovery_scheduler.stop()
        self.assertFalse(self.rt.recovery_readiness()['checks']['scheduler_running'])

    def test_healthz_stays_live_when_readyz_is_unavailable(self):
        handler = ba.Handler.__new__(ba.Handler)
        handler._send = lambda status, body: (status, body)
        with patch.object(ba, 'runtime', self.rt):
            handler.path = '/healthz'
            self.assertEqual(handler.do_GET()[0], 200)
            handler.path = '/readyz'
            self.assertEqual(handler.do_GET()[0], 503)

    def test_drill_http_budget_is_bounded_and_uncertain_writes_are_not_retried(self):
        from drill_recovery import RecoveryDrill, DrillFailure
        drill = RecoveryDrill(self.tmp.name, 30)
        drill.base = 'http://127.0.0.1:1'
        with patch.object(drill.opener, 'open', side_effect=TimeoutError) as request:
            with self.assertRaises(DrillFailure) as failure:
                drill.request('/api/confirm', {'private_body': 'NEVER_ECHO_THIS_BODY'})
            request.assert_called_once()
            self.assertEqual(request.call_args.kwargs['timeout'], 10)
            self.assertIn('/api/confirm', str(failure.exception))
            self.assertNotIn('NEVER_ECHO_THIS_BODY', str(failure.exception))
        from io import BytesIO
        limited = RecoveryDrill(self.tmp.name, .75)
        limited.base = drill.base
        response = BytesIO(b'{}')
        response.status = 200
        with patch.object(limited.opener, 'open', return_value=response) as request:
            self.assertEqual(limited.request('/readyz'), (200, {}))
            self.assertEqual(request.call_args.kwargs['timeout'], .75)

    @unittest.skipUnless(os.getenv('C8_RUN_DRILL') == '1', 'opt-in local subprocess/HTTP drill')
    def test_real_sigkill_drill(self):
        run = subprocess.run([sys.executable, str(ROOT / 'scripts/drill_recovery.py')],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stdout + run.stderr)
        report = json.loads(run.stdout)
        self.assertEqual(report['status'], 'PASS')
        self.assertEqual(report['core_transaction_count'], 2)
        self.assertTrue(report['expired_auth_rechallenged'])
        self.assertNotIn('123456', run.stdout)


if __name__ == '__main__':
    unittest.main()
