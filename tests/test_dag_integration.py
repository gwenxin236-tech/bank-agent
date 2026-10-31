"""D6 runtime acceptance: real Agent path, durable effects and authenticated controls."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import banking_agent as ba
from task_dag import DagExecutor


class DagIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'bank.sqlite3'
        self.runtimes = []
        self.rt = self.open()

    def tearDown(self):
        for rt in self.runtimes:
            rt.recovery_scheduler.stop()
            rt.bank.close()
            rt.audit.db.close()
        self.tmp.cleanup()

    def open(self):
        rt = ba.AgentRuntime(db_path=self.path, offline=True)
        self.runtimes.append(rt)
        return rt

    def control(self, action, dag, **kw):
        return self.rt.dag_control(action, dag, 'demo-user', 'demo-device', **kw)

    def birthday(self):
        p = self.rt.chat('我爱人生日是12月25日')['plan']
        out = self.rt.confirm(p['plan_id'], p['consent_token'])
        self.assertEqual(out['result']['status'], 'SUCCESS', out)
        task = out['result']['results'][0]['data']['task_id']
        return p, task, 'linkage-' + task

    def test_pending_confirmation_is_durable_gate_and_approve_requires_consent(self):
        p = self.rt.chat('给妈妈转账200元')['plan']
        dag = self.rt.dag_store.get('plan-' + p['plan_id'])
        self.assertEqual(dag.status, 'waiting_human')
        before = self.rt.bank.balance_cents
        denied = self.control('approve', dag.dag_id, action_token='bad')
        self.assertFalse(denied['ok'])
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertEqual(self.rt.dag_store.get(dag.dag_id).status, 'waiting_human')
        ok = self.control('approve', dag.dag_id, action_token=p['consent_token'])
        self.assertEqual(ok['result']['status'], 'SUCCESS')
        self.assertEqual(self.rt.bank.balance_cents, before - 20000)
        self.assertEqual(self.rt.dag_store.get(dag.dag_id).status, 'completed')

    def test_birthday_main_chain_uses_executor_and_legacy_table_is_read_only(self):
        before = self.rt.bank.balance_cents
        _, task, dag_id = self.birthday()
        dag = self.rt.dag_store.get(dag_id)
        self.assertEqual(dag.metadata['execution_owner'], 'dag')
        self.assertIsNone(self.rt.audit.get_linkage_task(task))
        self.assertEqual(self.rt.bank.balance_cents, before - 100000)
        self.assertTrue(any(e['event_type'] == 'dag.node.transition' for e in self.rt.audit.list_for_trace(dag.trace_id)))
        self.assertTrue(self.rt.audit.verify_trace(dag.trace_id)['valid'])

    def test_pause_restart_resume_delay_and_cancel_compensates_once(self):
        before = self.rt.bank.balance_cents
        _, task, dag_id = self.birthday()
        due = self.rt.get_linkage_task(task)['nodes'][1]['due_at']
        self.assertTrue(self.control('pause', dag_id)['ok'])
        self.rt = self.open()
        self.rt.restore_scheduled_tasks(now=due)
        self.assertEqual(self.rt.bank.linkage_orders, [])
        self.assertEqual(self.rt.bank.balance_cents, before - 100000)
        self.assertTrue(self.control('resume', dag_id)['ok'])
        self.rt.restore_scheduled_tasks(now=due)
        self.rt.restore_scheduled_tasks(now=due)
        self.assertEqual(len(self.rt.bank.linkage_orders), 1)
        self.assertTrue(self.control('cancel', dag_id)['ok'])
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertTrue(self.control('cancel', dag_id)['ok'])
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.rt = self.open()
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertEqual(len(self.rt.bank.linkage_orders), 1)
        self.assertEqual(self.rt.dag_store.get(dag_id).status, 'cancelled')

    def test_cross_user_and_device_controls_denied_with_audit(self):
        _, _, dag_id = self.birthday()
        before = self.rt.dag_store.get(dag_id).to_dict()
        for action in ('state', 'pause', 'resume', 'cancel', 'approve'):
            for actor, device in [('attacker', 'demo-device'), ('demo-user', 'other-device')]:
                out = self.rt.dag_control(action, dag_id, actor, device)
                self.assertFalse(out['ok'])
                self.assertEqual(out['error_code'], 'AUTHZ_DENIED')
        self.assertEqual(self.rt.dag_store.get(dag_id).to_dict(), before)
        self.assertTrue(any(e['event_type'] == 'authz.denied' for e in self.rt.audit.list(100)))

    def test_human_handoff_does_not_execute_or_allow_later_approval(self):
        p = self.rt.chat('给妈妈转账200元')['plan']
        dag_id = 'plan-' + p['plan_id']
        before = self.rt.bank.balance_cents
        out = self.control('approve', dag_id, decision='handoff')
        self.assertTrue(out['ok'])
        self.assertEqual(self.rt.dag_store.get(dag_id).status, 'handoff')
        out = self.rt.confirm(p['plan_id'], p['consent_token'])
        self.assertNotEqual(out.get('result', {}).get('status'), 'SUCCESS')
        self.assertEqual(self.rt.bank.balance_cents, before)

    def test_timeout_restart_queries_core_without_replaying_transfer(self):
        p = self.rt.chat('给妈妈转账200元')['plan']
        self.rt.bank.inject_fault('timeout')
        result = self.rt.confirm(p['plan_id'], p['consent_token'])
        self.assertEqual(result['result']['status'], 'PROCESSING')
        self.assertIn('银行还在确认', result['message'])
        self.rt = self.open()
        with patch.object(self.rt.bank, 'transfer', side_effect=AssertionError('no retry')):
            self.rt.reconcile_recovery_tasks()
        self.assertEqual(self.rt.bank.db.execute('SELECT count(*) FROM core_transactions').fetchone()[0], 1)
        self.assertNotEqual(self.rt.dag_store.get('plan-' + p['plan_id']).status, 'failed')

    def test_order_failure_fail_fast_and_lock_compensation(self):
        before = self.rt.bank.balance_cents
        _, task, dag_id = self.birthday()
        due = self.rt.get_linkage_task(task)['nodes'][1]['due_at']
        with patch.object(self.rt.bank, 'order_linkage_item', return_value=ba.ToolResult(False, 'order_linkage_item', error_code='DENIED')) as call:
            self.rt.restore_scheduled_tasks(now=due)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(self.rt.dag_store.get(dag_id).status, 'failed')
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertEqual(self.rt.bank.linkage_orders, [])

    def test_restart_waiting_confirmation_never_dispatches(self):
        p = self.rt.chat('给妈妈转账200元')['plan']
        self.rt = self.open()
        before = self.rt.bank.balance_cents
        with patch.object(self.rt.bank, 'transfer', side_effect=AssertionError('no auto-confirm')):
            self.rt.reconcile_recovery_tasks()
        self.assertEqual(self.rt.dag_store.get('plan-' + p['plan_id']).status, 'waiting_human')
        self.assertEqual(self.rt.bank.balance_cents, before)

    def test_generic_pause_resume_requires_fresh_confirmation_skips_completed_nodes(self):
        p = self.rt.chat('给妈妈转账200元')['plan']
        dag_id = 'plan-' + p['plan_id']
        before = self.rt.bank.balance_cents
        original = self.rt._execute_step
        def pause_after_read(plan, step):
            result = original(plan, step)
            if step.tool == 'resolve_beneficiary':
                self.rt._dag_pause_requests.add(dag_id)
            return result
        with patch.object(self.rt, '_execute_step', side_effect=pause_after_read):
            out = self.rt.confirm(p['plan_id'], p['consent_token'])
        self.assertEqual(out['result']['status'], 'PAUSED')
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.rt = self.open()
        resumed = self.control('resume', dag_id)
        self.assertTrue(resumed['ok'], resumed)
        self.assertNotEqual(resumed['plan']['consent_token'], p['consent_token'])
        self.assertEqual(self.rt.dag_store.get(dag_id).status, 'waiting_human')
        self.assertFalse(self.rt.confirm(p['plan_id'], p['consent_token'])['ok'])
        with patch.object(self.rt.bank, 'resolve_beneficiary', side_effect=AssertionError('completed read cannot rerun')):
            out = self.control('approve', dag_id, action_token=resumed['plan']['consent_token'])
        self.assertEqual(out['result']['status'], 'SUCCESS', out)
        self.assertEqual(self.rt.bank.balance_cents, before - 20000)

    def test_expired_auth_after_restart_cannot_be_bypassed_by_dag_approve(self):
        from datetime import timedelta
        p = self.rt.chat('给妈妈转账5000元')['plan']
        dag_id = 'plan-' + p['plan_id']
        cid = self.rt.confirm(p['plan_id'], p['consent_token'])['auth_challenge']['credential_id']
        self.assertTrue(self.rt.verify(p['plan_id'], p['consent_token'], '123456', cid)['ok'])
        self.rt.auth_credentials.get(cid).expires_at -= timedelta(minutes=6)
        self.rt.save_task_checkpoint(self.rt.pending[p['plan_id']], 'waiting_confirmation')
        self.rt = self.open()
        before = self.rt.bank.balance_cents
        out = self.control('approve', dag_id, action_token=p['consent_token'], credential_id=cid)
        self.assertEqual(out['error_code'], 'AUTH_EXPIRED')
        self.assertEqual(self.rt.bank.balance_cents, before)
        fresh = self.control('approve', dag_id, action_token=p['consent_token'])
        self.assertTrue(fresh['needs_auth'])
        self.assertNotEqual(fresh['auth_challenge']['credential_id'], cid)

    def test_crash_after_debit_before_checkpoint_queries_without_second_effect(self):
        import sqlite3
        p = self.rt.chat('给妈妈转账200元')['plan']
        original = self.rt.dag_store.checkpoint
        def crash(snapshot, event):
            if event.get('node_id') == 's3' and event.get('to') == 'succeeded':
                raise sqlite3.OperationalError('injected checkpoint crash')
            original(snapshot, event)
        before = self.rt.bank.balance_cents
        with patch.object(self.rt.dag_store, 'checkpoint', side_effect=crash):
            out = self.rt.confirm(p['plan_id'], p['consent_token'])
        self.assertEqual(out['error_code'], 'RECOVERY_UNAVAILABLE')
        with patch.object(ba.MockBankCore, 'transfer', side_effect=AssertionError('no retry after crash')):
            self.rt = self.open()
            self.rt.reconcile_recovery_tasks()
        self.assertEqual(self.rt.bank.balance_cents, before - 20000)
        self.assertEqual(self.rt.bank.db.execute('SELECT count(*) FROM core_transactions').fetchone()[0], 1)
        self.assertEqual(self.rt.dag_store.get('plan-' + p['plan_id']).status, 'completed')

    def test_restart_completed_birthday_nodes_uses_persisted_evidence_not_tool_calls(self):
        _, task, dag_id = self.birthday()
        self.rt.restore_scheduled_tasks(now=self.rt.get_linkage_task(task)['nodes'][1]['due_at'])
        order = copy.deepcopy(self.rt.bank.linkage_orders)
        balance = self.rt.bank.balance_cents
        with patch.object(ba.MockBankCore, 'lock_subaccount', side_effect=AssertionError('no relock')), \
             patch.object(ba.MockBankCore, 'order_linkage_item', side_effect=AssertionError('no reorder')):
            self.rt = self.open()
        self.assertEqual(self.rt.bank.balance_cents, balance)
        self.assertEqual(self.rt.bank.linkage_orders, order)

    def test_human_rejection_has_no_effect_and_is_audited(self):
        p = self.rt.chat('给妈妈转账200元')['plan']
        before = self.rt.bank.balance_cents
        out = self.control('approve', 'plan-' + p['plan_id'], decision='reject')
        self.assertTrue(out['ok'])
        self.assertEqual(out['status'], 'failed')
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertTrue(self.rt.audit.verify_trace(p['trace_id'])['valid'])

    def test_original_cancel_route_also_closes_dag_and_survives_restart(self):
        p = self.rt.chat('给妈妈转账200元')['plan']
        before = self.rt.bank.balance_cents
        out = self.rt.cancel(p['plan_id'], p['consent_token'])
        self.assertTrue(out['ok'])
        self.assertEqual(self.rt.dag_store.get('plan-' + p['plan_id']).status, 'cancelled')
        self.rt = self.open()
        self.assertEqual(self.rt.bank.balance_cents, before)
        self.assertFalse(self.rt.confirm(p['plan_id'], p['consent_token'])['ok'])
        self.assertEqual(self.rt.dag_store.get('plan-' + p['plan_id']).status, 'cancelled')
        self.assertTrue(self.rt.audit.verify_trace(p['trace_id'])['valid'])

    def test_paused_result_never_claims_transfer_finished(self):
        p = self.rt.chat('给妈妈转账200元')['plan']
        plan = self.rt.pending[p['plan_id']]
        result = ba.present_result(plan, {'ok': True, 'status': 'PAUSED', 'results': []})
        self.assertEqual(result['status'], 'PAUSED')
        self.assertIn('暂停', result['user_message'])
        self.assertNotIn('已完成', result['user_message'])

    def test_http_routes_require_session_and_enforce_owner_without_leaking_state(self):
        import http.client
        from http.server import ThreadingHTTPServer
        import threading
        class QuietHandler(ba.Handler):
            def log_message(self, *args):
                pass
        _, _, dag_id = self.birthday()
        self.rt.family.demo_sessions['outsider'] = {'user_id': 'other', 'device_id': 'other-device', 'role': 'user'}
        with patch.object(ba, 'runtime', self.rt), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
            with ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as server:
                thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
                thread.start()
                def request(path, token='', data=None):
                    c = http.client.HTTPConnection(*server.server_address, timeout=5)
                    headers = {'Content-Type': 'application/json'}
                    if token:
                        headers['Authorization'] = 'Bearer ' + token
                    try:
                        c.request('POST' if data is not None else 'GET', path, json.dumps(data) if data is not None else None, headers)
                        response = c.getresponse()
                        return response.status, json.loads(response.read())
                    finally:
                        c.close()
                try:
                    for token in ('', 'outsider'):
                        self.assertEqual(request('/api/dag/state?dag_id=' + dag_id, token)[0], 403)
                        for action in ('pause', 'resume', 'cancel', 'approve'):
                            status, out = request('/api/dag/' + action, token, {'dag_id': dag_id})
                            self.assertEqual(status, 403)
                            self.assertFalse(out['ok'])
                    status, out = request('/api/dag/state?dag_id=' + dag_id, 'demo-user-session')
                    self.assertEqual(status, 200)
                    self.assertEqual(out['status'], 'running')
                    self.assertNotIn('metadata', out)
                    self.assertNotIn('consent_token', json.dumps(out))
                    self.assertEqual(request('/api/dag/pause', 'demo-user-session', {'dag_id': dag_id})[1]['status'], 'paused')
                    self.assertEqual(request('/api/dag/resume', 'demo-user-session', {'dag_id': dag_id})[1]['status'], 'running')
                    self.assertEqual(request('/api/dag/cancel', 'demo-user-session', {'dag_id': dag_id})[1]['status'], 'cancelled')
                finally:
                    server.shutdown()
                    thread.join(timeout=5)


if __name__ == '__main__':
    unittest.main()
