"""E2: delivery uncertainty is not bank failure; reconciliation never resubmits."""
import importlib
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import banking_agent as ba


class UnknownReceiptTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'bank.sqlite3'
        self.runtimes = []
        self.rt = self.open()

    def open(self):
        rt = ba.AgentRuntime(db_path=self.path, offline=True)
        self.runtimes.append(rt)
        return rt

    def tearDown(self):
        for rt in self.runtimes:
            rt.recovery_scheduler.stop()
            rt.bank.close()
            rt.audit.db.close()
        self.tmp.cleanup()

    def plan(self):
        return self.rt.chat('给妈妈转 200 元')['plan']

    def confirm(self, plan):
        return self.rt.confirm(plan['plan_id'], plan['consent_token'])

    def status(self, plan):
        return self.rt.task_status(plan['plan_id'], plan['consent_token'], 'demo-user', 'demo-device')

    def row(self, plan):
        return self.rt.bank._row_for_idempotency('idem:' + plan['plan_id'] + ':create_transfer')

    def assert_unknown(self, out):
        self.assertEqual(out['result']['status'], 'UNKNOWN', out)
        self.assertEqual(out['result_view']['status'], 'UNKNOWN')
        self.assertIn('结果未知，正在核验', out['message'])
        self.assertNotIn('未发生扣款', out['message'])
        self.assertNotIn('失败', out['message'])
        self.assertFalse(out['plan_view']['requires_confirmation'])

    def test_view_distinguishes_observation_and_core_status_without_mutating_input(self):
        views = importlib.import_module('transaction_views')
        for state, label in [('processing', '处理中'), ('unknown', '结果未知'),
                             ('succeeded', '已完成'), ('failed', '明确失败'),
                             ('partial', '部分完成'), ('reversed', '已冲正')]:
            data = {'status': state}
            before = dict(data)
            view = views.transaction_view(data, 'trace-test')
            self.assertEqual(view['title'], label)
            self.assertEqual(view['trace_id'], 'trace-test')
            self.assertEqual(data, before)
        self.assertEqual(views.transaction_view({'status': 'unrecognized'})['status'], 'UNKNOWN')

    def test_transport_timeout_unknown_then_success_queries_original_request(self):
        self.rt.bank.inject_fault('transport_timeout')
        p = self.plan()
        before = self.rt.bank.balance_cents
        with patch.object(self.rt.bank, 'transfer', wraps=self.rt.bank.transfer) as submit:
            self.assert_unknown(self.confirm(p))
            row = self.row(p)
            self.assertEqual(row['status'], 'processing')  # C1 truth unchanged
            self.assertEqual(self.rt.bank.balance_cents, before)
            for _ in range(3):
                self.assertEqual(self.status(p)['status'], 'unknown')
                self.assertFalse(self.confirm(p)['ok'])
            self.assertEqual(submit.call_count, 1)
            self.rt.bank.finalize_transaction(row['transaction_id'], 'succeeded')
            self.assertEqual(self.status(p)['status'], 'succeeded')
            self.rt.reconcile_recovery_tasks()
            self.assertEqual(submit.call_count, 1)
        self.assertEqual(self.rt.bank.balance_cents, before - 20000)
        self.assertEqual(self.rt.bank.db.execute('SELECT count(*) FROM core_transactions').fetchone()[0], 1)
        events = self.rt.audit.list_for_trace(p['trace_id'])
        self.assertTrue(any(e['event_type'] == 'transaction.unknown' for e in events))
        self.assertTrue(any(e['event_type'] == 'task.recovery.reconciled' and e['payload']['status'] == 'succeeded' for e in events))
        self.assertTrue(self.rt.verify_audit_trace(p['trace_id'])['valid'])

    def test_network_unreachable_survives_restart_and_reconciles_failure_without_debit(self):
        self.rt.bank.inject_fault('network_unreachable')
        p = self.plan()
        before = self.rt.bank.balance_cents
        self.assert_unknown(self.confirm(p))
        tx = self.row(p)['transaction_id']
        self.rt = self.open()
        with patch.object(self.rt.bank, 'transfer', side_effect=AssertionError('must never resend')):
            self.rt.reconcile_recovery_tasks()
            self.assertEqual(self.status(p)['status'], 'unknown')
            self.assertEqual(self.rt.bank.balance_cents, before)
            self.rt.bank.finalize_transaction(tx, 'failed')
            self.assertEqual(self.status(p)['status'], 'failed')
            self.rt.reconcile_recovery_tasks()
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertEqual(self.rt.dag_store.get('plan-' + p['plan_id']).status, 'failed')

    def test_reachable_processing_is_not_unknown(self):
        self.rt.bank.inject_fault('processing')
        p = self.plan()
        out = self.confirm(p)
        self.assertEqual(out['result_view']['status'], 'PROCESSING')
        self.assertNotIn('结果未知', out['result_view']['user_message'])
        self.assertEqual(self.status(p)['status'], 'processing')

    def test_restored_transport_without_final_result_only_becomes_processing(self):
        self.rt.bank.inject_fault('transport_timeout')
        p = self.plan()
        before = self.rt.bank.balance_cents
        self.assert_unknown(self.confirm(p))
        self.rt.bank.inject_fault(None)  # trusted demo control, not a user API
        with patch.object(self.rt.bank, 'transfer', side_effect=AssertionError('no resend')):
            self.assertEqual(self.status(p)['status'], 'processing')
        self.assertEqual(self.rt.bank.balance_cents, before)

    def test_real_submit_timeout_before_core_record_is_unknown_never_failed_or_retried(self):
        p = self.plan()
        before = self.rt.bank.balance_cents
        with patch.object(self.rt.bank, 'transfer', side_effect=TimeoutError('secret-do-not-log')) as submit:
            self.assert_unknown(self.confirm(p))
            self.rt.reconcile_recovery_tasks()
            self.assertEqual(self.status(p)['status'], 'unknown')
            self.assertEqual(submit.call_count, 1)
        self.assertIsNone(self.row(p))
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertNotIn('secret-do-not-log', json.dumps(self.rt.audit.list_for_trace(p['trace_id'])))

    def test_timeout_after_core_commit_never_claims_no_debit_and_queries_without_resend(self):
        p = self.plan()
        before = self.rt.bank.balance_cents
        original = self.rt.bank.transfer
        def committed_then_lost(*args, **kwargs):
            original(*args, **kwargs)
            raise TimeoutError('lost acknowledgement')
        with patch.object(self.rt.bank, 'transfer', side_effect=committed_then_lost) as submit:
            self.assert_unknown(self.confirm(p))
            self.assertEqual(self.rt.bank.balance_cents, before - 20000)
            self.assertEqual(self.status(p)['status'], 'succeeded')
            self.rt.reconcile_recovery_tasks()
            self.assertEqual(submit.call_count, 1)

    def test_query_exception_and_negative_query_response_override_submission_success(self):
        for fault in (TimeoutError('query secret'),
                      ba.ToolResult(False, 'query_transaction', error_code='CORE_UNREACHABLE')):
            with self.subTest(fault=type(fault).__name__):
                p = self.plan()
                kwargs = {'side_effect': fault} if isinstance(fault, Exception) else {'return_value': fault}
                with patch.object(self.rt.bank, 'query_transaction', **kwargs):
                    self.assert_unknown(self.confirm(p))
                    self.rt.reconcile_recovery_tasks()
                    self.assertEqual(self.status(p)['status'], 'unknown')
                with patch.object(self.rt.bank, 'transfer', side_effect=AssertionError('no retry')):
                    self.assertEqual(self.status(p)['status'], 'succeeded')

    def test_unknown_path_works_with_dag_disabled(self):
        self.rt.dag_store = None
        self.rt.bank.inject_fault('transport_timeout')
        p = self.plan()
        self.assert_unknown(self.confirm(p))
        self.rt.bank.finalize_transaction(self.row(p)['transaction_id'], 'succeeded')
        self.assertEqual(self.status(p)['status'], 'succeeded')

    def test_status_query_has_same_unknown_view_and_cannot_be_read_by_other_user(self):
        self.rt.bank.inject_fault('network_unreachable')
        p = self.plan()
        self.assert_unknown(self.confirm(p))
        tx = self.row(p)['transaction_id']
        self.assertEqual(self.rt.query_transaction(tx)['result_view']['status'], 'UNKNOWN')
        for actor, device in [('other', 'demo-device'), ('demo-user', 'other-device')]:
            denied = self.rt.task_status(p['plan_id'], p['consent_token'], actor, device)
            self.assertEqual(denied['error_code'], 'TASK_ACCESS_DENIED')
            self.assertNotIn('result_view', denied)

    def test_unknown_does_not_fail_or_compensate_dag_node(self):
        self.rt.bank.inject_fault('transport_timeout')
        p = self.plan()
        self.assert_unknown(self.confirm(p))
        dag = self.rt.dag_store.get('plan-' + p['plan_id'])
        self.assertEqual(dag.status, 'waiting_human')
        transfer = next(n for n in dag.nodes if n.tool == 'create_transfer')
        self.assertEqual(transfer.status, 'running')
        self.assertNotIn('compensation', transfer.result)
        self.assertEqual(transfer.result['execution']['output']['status'], 'UNKNOWN')


if __name__ == '__main__':
    unittest.main()
