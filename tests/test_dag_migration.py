"""D6 conversion/storage, legacy import and execution-owner safe flag rollback."""
import copy
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'app'))
import banking_agent as ba
from task_dag import (DagExecutor, DagNode, TaskDag, TaskDagStore,
                      plan_to_dag, linkage_to_dag, migrate_linkage_to_dag)

ROOT = Path(__file__).resolve().parents[1]


def legacy_task(task_id='birthday-test', state='ACTIVE', statuses=('COMPLETED', 'PENDING', 'PENDING')):
    task = {'task_id': task_id, 'trace_id': 'trace-' + task_id, 'actor_id': 'alice',
            'status': state, 'recipient': '爱人', 'birthday_date': '2099-12-25',
            'lock_amount_cents': 100000, 'created_at': '2026-09-22T00:00:00+00:00',
            'nodes': []}
    for name, action, due, status in zip('ABC', ('lock_subaccount', 'order_flowers', 'order_cake'),
                                      ('2026-09-22', '2099-12-23', '2099-12-24'), statuses):
        node = dict(node_id=name, action=action, due_at=due + 'T00:00:00+00:00', status=status)
        if status in ('COMPLETED', 'COMPENSATED'):
            node['completed_at'] = node['due_at']
            node['result'] = ({'amount_cents': 100000, 'status': 'LOCKED'} if name == 'A'
                              else {'order_id': 'order-' + name, 'status': 'ORDERED'})
        if status == 'COMPENSATED':
            node['compensation'] = {'amount_cents': 100000, 'status': 'COMPENSATED'}
        if status == 'FAILED':
            node['error'] = '模拟失败'
        task['nodes'].append(node)
    return task


class DagConversionTests(unittest.TestCase):
    def plan(self, strong=False):
        return ba.AgentPlan('plan-test', 'trace-test', 'transfer', '测试', 'red' if strong else 'yellow',
                            True, strong, {'amount_cents': 300000 if strong else 20000, 'beneficiary': '妈妈'},
                            [ba.PlanStep('s1', '核验', 'resolve_beneficiary'),
                             ba.PlanStep('s2', '转账', 'create_transfer')], ba.now_iso(),
                            consent_token='NEVER_PERSIST_THIS_TOKEN', actor_id='alice', device_id='device-a')

    def test_linear_conversion_chain_identity_integer_cents_and_confirmation_gate(self):
        plan = self.plan(True)
        dag = plan_to_dag(plan)
        self.assertEqual((dag.plan_id, dag.trace_id, dag.actor_id), ('plan-test', 'trace-test', 'alice'))
        self.assertEqual([n.node_type for n in dag.nodes], ['human_gate', 'action', 'action'])
        self.assertEqual(dag.nodes[1].depends_on, [dag.nodes[0].node_id])
        self.assertEqual(dag.nodes[2].depends_on, ['s1'])
        self.assertTrue(dag.nodes[0].params['requires_strong_auth'])
        self.assertEqual(dag.nodes[2].params['amount_cents'], 300000)
        self.assertIs(type(dag.nodes[2].params['amount_cents']), int)
        self.assertNotIn(plan.consent_token, json.dumps(dag.to_dict()))
        self.assertEqual(plan_to_dag(plan).to_dict(), dag.to_dict())
        calls = []
        DagExecutor(dag, lambda *args: calls.append(args)).run()
        self.assertEqual(dag.status, 'waiting_human')
        self.assertEqual(calls, [])
        self.assertEqual([s.status for s in plan.steps], ['pending', 'pending'])

    def test_green_linear_conversion_has_no_artificial_gate(self):
        plan = self.plan()
        plan.requires_confirmation = False
        plan.requires_strong_auth = False
        dag = plan_to_dag(plan)
        self.assertEqual([n.node_id for n in dag.nodes], ['s1', 's2'])
        self.assertEqual(dag.nodes[0].depends_on, [])
        self.assertEqual(dag.nodes[1].depends_on, ['s1'])

    def test_completed_step_is_preserved_but_processing_is_not_success(self):
        plan = self.plan()
        plan.requires_confirmation = False
        plan.steps[0].status = 'completed'
        plan.steps[0].detail = json.dumps({'name': '妈妈'})
        plan.steps[1].status = 'completed'
        plan.steps[1].detail = json.dumps({'status': 'processing', 'transaction_id': 'tx-1'})
        dag = plan_to_dag(plan)
        self.assertEqual([n.status for n in dag.nodes], ['succeeded', 'running'])
        self.assertEqual(dag.nodes[1].result['execution']['data']['transaction_id'], 'tx-1')
        calls = []
        DagExecutor(dag, lambda *args: calls.append(args)).run()
        self.assertEqual(calls, [])
        self.assertEqual(dag.status, 'waiting_human')

    def test_birthday_conversion_has_explicit_independent_delays_and_compensation(self):
        task = legacy_task()
        before = copy.deepcopy(task)
        dag = linkage_to_dag(task)
        nodes = {n.node_id: n for n in dag.nodes}
        self.assertEqual(set(nodes), {'A', 'delay-B', 'B', 'delay-C', 'C'})
        self.assertEqual(nodes['delay-B'].params['run_at'], task['nodes'][1]['due_at'])
        self.assertEqual(nodes['delay-C'].params['run_at'], task['nodes'][2]['due_at'])
        self.assertEqual(nodes['delay-B'].depends_on, ['A'])
        self.assertEqual(nodes['delay-C'].depends_on, ['A'])
        self.assertEqual(nodes['B'].depends_on, ['delay-B'])
        self.assertEqual(nodes['C'].depends_on, ['delay-C'])
        self.assertEqual(nodes['A'].status, 'succeeded')
        self.assertEqual(nodes['A'].compensation, {'tool': 'compensate_subaccount_lock', 'params': {'task_id': task['task_id']}})
        self.assertEqual(nodes['A'].params['amount_cents'], 100000)
        self.assertEqual(dag.metadata['legacy_state'], task)
        self.assertEqual(task, before)
        self.assertEqual(dag.metadata['execution_owner'], 'legacy')
        self.assertTrue(all(n.idempotency_key == f'idem:{dag.dag_id}:{n.node_id}' for n in dag.nodes))

    def test_terminal_and_partial_legacy_states_preserve_order_ids_and_evidence(self):
        cases = [('COMPLETED', ('COMPLETED',) * 3, 'completed'),
                 ('CANCELLED', ('COMPENSATED', 'CANCELLED', 'CANCELLED'), 'cancelled'),
                 ('CANCELLED', ('COMPENSATED', 'COMPLETED', 'CANCELLED'), 'cancelled'),
                 ('ACTIVE', ('COMPLETED', 'FAILED', 'PENDING'), 'running')]
        mapping = {'COMPLETED': 'succeeded', 'COMPENSATED': 'compensated',
                   'CANCELLED': 'skipped', 'PENDING': 'pending', 'FAILED': 'failed'}
        for state, statuses, target in cases:
            with self.subTest(state=state, statuses=statuses):
                old = legacy_task(state=state, statuses=statuses)
                dag = linkage_to_dag(old)
                self.assertEqual(dag.status, target)
                nodes = {n.node_id: n for n in dag.nodes}
                self.assertEqual([nodes[n].status for n in 'ABC'], [mapping[x] for x in statuses])
                self.assertEqual(TaskDag.from_dict(dag.to_dict()).metadata['legacy_state'], old)
                for item in old['nodes']:
                    if 'result' in item:
                        self.assertEqual(nodes[item['node_id']].result['execution']['data'], item['result'])
                    if 'compensation' in item:
                        self.assertEqual(nodes[item['node_id']].result['compensation']['result']['data'], item['compensation'])

    def test_invalid_legacy_data_is_not_guessed_or_coerced(self):
        changes = [lambda t: t.update(lock_amount_cents=1.5), lambda t: t.update(actor_id=''),
                   lambda t: t['nodes'][1].update(status='UNKNOWN'),
                   lambda t: t['nodes'][1].update(action='create_transfer'),
                   lambda t: t['nodes'][1].update(due_at='bad'), lambda t: t['nodes'].pop()]
        for mutate in changes:
            task = legacy_task()
            mutate(task)
            with self.subTest(task=task), self.assertRaises(ValueError):
                linkage_to_dag(task)


class DagMigrationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        self.audit = ba.AuditStore(self.db)
        self.addCleanup(self.audit.db.close)
        self.store = TaskDagStore(self.audit)

    def test_migration_is_insert_only_idempotent_and_never_dispatches_bank_tools(self):
        task = legacy_task()
        self.audit.save_linkage_task(task)
        with patch.object(ba.MockBankCore, 'lock_subaccount', side_effect=AssertionError('no bank calls')), \
             patch.object(ba.MockBankCore, 'order_linkage_item', side_effect=AssertionError('no bank calls')):
            ids = migrate_linkage_to_dag(self.audit, self.store)
            self.assertEqual(len(ids), 1)
            first = self.store.get(ids[0]).to_dict()
            events = self.audit.list_for_trace(task['trace_id'])
            self.assertEqual(migrate_linkage_to_dag(self.audit, self.store), [])
            self.assertEqual(self.store.get(ids[0]).to_dict(), first)
            self.assertEqual(self.audit.list_for_trace(task['trace_id']), events)
        self.assertEqual(self.audit.get_linkage_task(task['task_id']), task)
        self.assertEqual(events[-1]['event_type'], 'dag.migrated')
        self.assertEqual(events[-1]['actor'], 'alice')
        self.assertTrue(self.audit.verify_trace(task['trace_id'])['valid'])
        # Rerunning a migration cannot erase a newer executor checkpoint.
        dag = self.store.get(ids[0])
        dag.status = 'paused'
        self.store.save(dag, {'event_type': 'dag.transition', 'from': 'running', 'to': 'paused'})
        migrate_linkage_to_dag(self.audit, self.store)
        self.assertEqual(self.store.get(ids[0]).status, 'paused')

    def test_checkpoint_and_audit_commit_together_and_restore_full_states(self):
        for state in ('running', 'paused', 'waiting_human'):
            dag = TaskDag('dag-' + state, 'plan-' + state, 'test', 'trace-' + state, 'alice',
                          [DagNode('a', tool='get_balance')], status=state)
            self.store.save(dag, {'event_type': 'dag.created'})
        recovered = self.store.list_recoverable()
        self.assertEqual({d.status for d in recovered}, {'running', 'paused', 'waiting_human'})
        dag = self.store.get('dag-running')
        DagExecutor(dag, lambda *_: {'ok': True, 'data': {'amount_cents': 100}},
                    on_change=self.store.checkpoint).run()
        other = ba.AuditStore(self.db)
        try:
            saved = TaskDagStore(other).get(dag.dag_id)
            self.assertEqual(saved.to_dict(), dag.to_dict())
            self.assertEqual(saved.status, 'completed')
            self.assertTrue(other.verify_trace(dag.trace_id)['valid'])
            events = other.list_for_trace(dag.trace_id)
            self.assertEqual(len([e for e in events if e['event_type'].endswith('transition')]), 4)
        finally:
            other.db.close()

    def test_audit_failure_rolls_back_checkpoint_and_stops_before_dispatch(self):
        dag = TaskDag('dag-test', 'plan-test', 'test', 'trace-test', 'alice', [DagNode('a', tool='get_balance')])
        self.store.save(dag, {'event_type': 'dag.created'})
        before = self.store.get(dag.dag_id).to_dict()
        calls = []
        with patch.object(self.audit, 'write', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                DagExecutor(dag, lambda *args: calls.append(args), on_change=self.store.checkpoint).run()
        self.assertEqual(calls, [])
        self.assertEqual(self.store.get(dag.dag_id).to_dict(), before)
        self.assertTrue(self.audit.verify_trace(dag.trace_id)['valid'])

    def test_unchanged_legacy_source_does_not_erase_newer_checkpoint(self):
        task = legacy_task()
        self.audit.save_linkage_task(task)
        dag_id = migrate_linkage_to_dag(self.audit, self.store)[0]
        dag = self.store.get(dag_id)
        dag.transition('paused')
        self.store.save(dag, {'event_type': 'dag.transition', 'from': 'running', 'to': 'paused'})
        events = self.audit.list_for_trace(dag.trace_id)
        self.assertFalse(self.store.sync_linkage(task))
        self.assertEqual(self.store.get(dag_id).status, 'paused')
        self.assertEqual(self.audit.list_for_trace(dag.trace_id), events)

    def test_batch_migration_rolls_back_state_and_hash_events_on_late_failure(self):
        self.audit.save_linkage_task(legacy_task('first'))
        self.audit.save_linkage_task(legacy_task('second'))
        before = [tuple(r) for r in self.audit.db.execute('SELECT * FROM linkage_tasks ORDER BY task_id')]
        write = self.audit.write
        calls = []
        def fail_after_write(*args, **kwargs):
            write(*args, **kwargs)
            calls.append(args)
            if len(calls) == 2:
                raise OSError('simulated audit failure after insert')
        with patch.object(self.audit, 'write', side_effect=fail_after_write):
            with self.assertRaises(OSError):
                migrate_linkage_to_dag(self.audit, self.store)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.audit.db.execute('SELECT COUNT(*) FROM task_dags').fetchone()[0], 0)
        self.assertEqual(self.audit.list(100), [])
        self.assertEqual([tuple(r) for r in self.audit.db.execute('SELECT * FROM linkage_tasks ORDER BY task_id')], before)
        self.assertEqual(len(migrate_linkage_to_dag(self.audit, self.store)), 2)
        self.assertTrue(self.audit.verify_trace('trace-second')['valid'])

    def test_migration_keeps_legacy_rows_byte_for_byte_and_audit_omits_private_results(self):
        task = legacy_task()
        task['nodes'][0]['result']['phone'] = '13800138000'
        self.audit.save_linkage_task(task)
        before = tuple(self.audit.db.execute('SELECT * FROM linkage_tasks').fetchone())
        dag_id = migrate_linkage_to_dag(self.audit, self.store)[0]
        self.assertEqual(tuple(self.audit.db.execute('SELECT * FROM linkage_tasks').fetchone()), before)
        self.assertEqual(self.store.get(dag_id).metadata['legacy_state'], task)
        self.assertNotIn('13800138000', json.dumps(self.audit.list_for_trace(task['trace_id'])))
        self.assertTrue(self.audit.verify_trace(task['trace_id'])['valid'])

    def test_legacy_sync_cannot_overwrite_executor_owned_dag(self):
        task = legacy_task()
        self.audit.save_linkage_task(task)
        dag_id = migrate_linkage_to_dag(self.audit, self.store)[0]
        dag = self.store.get(dag_id)
        dag.metadata['execution_owner'] = 'dag'
        self.store.save(dag, {'event_type': 'dag.adopted'})
        changed = copy.deepcopy(task)
        changed['nodes'][1]['status'] = 'CANCELLED'
        with self.assertRaises(ValueError):
            self.store.sync_linkage(changed)
        self.assertEqual(self.store.get(dag_id).to_dict(), dag.to_dict())

    def test_saved_identity_is_immutable_and_legacy_sync_is_atomic(self):
        self.audit.linkage_dag_store = self.store
        task = legacy_task()
        self.audit.save_linkage_task(task)
        dag_id = linkage_to_dag(task).dag_id
        changed = copy.deepcopy(task)
        changed['actor_id'] = 'mallory'
        with self.assertRaises(ValueError):
            self.audit.save_linkage_task(changed)
        self.assertEqual(self.audit.get_linkage_task(task['task_id']), task)
        self.assertEqual(self.store.get(dag_id).actor_id, 'alice')


class DagFeatureFallbackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = Path(self.tmp.name) / 'state.sqlite3'
        self.config = Path(self.tmp.name) / 'features.json'
        self.options = json.loads((ROOT / 'config/features.json').read_text(encoding='utf-8'))
        self.runtimes = []
        self.addCleanup(self.close)

    def close(self):
        for rt in self.runtimes:
            rt.bank.close()
            rt.audit.db.close()

    def runtime(self, enabled):
        self.options['dag'] = {'enabled': enabled}
        self.config.write_text(json.dumps(self.options), encoding='utf-8')
        rt = ba.AgentRuntime(db_path=self.db, offline=True, features_path=self.config)
        self.runtimes.append(rt)
        return rt

    def create(self, rt):
        planned = rt.chat('我爱人生日是12月25日')
        self.assertEqual(planned['plan']['intent'], 'birthday_linkage')
        out = rt.confirm(planned['plan']['plan_id'], planned['plan']['consent_token'])
        self.assertEqual(out['result']['status'], 'SUCCESS')
        return out['result']['results'][0]['data']['task_id']

    def test_flag_false_uses_only_legacy_storage_and_business_execution(self):
        with patch.object(ba, 'migrate_linkage_to_dag', side_effect=AssertionError('DAG disabled')):
            rt = self.runtime(False)
            task_id = self.create(rt)
            rt.restore_scheduled_tasks(now=rt.get_linkage_task(task_id)['nodes'][1]['due_at'].replace('00:00:00', '12:00:00'))
            self.assertEqual(len(rt.bank.linkage_orders), 1)
            self.assertTrue(rt.cancel_linkage_task(task_id)['ok'])
        self.assertIsNone(rt.dag_store)
        self.assertFalse(rt.audit.db.execute("SELECT 1 FROM sqlite_master WHERE name='task_dags'").fetchone())
        self.assertEqual(rt.get_linkage_task(task_id)['status'], 'CANCELLED')
        self.assertEqual(rt.bank.locked_subaccount_cents, 0)
        self.assertFalse(any(e['event_type'].startswith('dag.') for e in rt.audit.list(200)))

    def test_enabled_birthday_uses_dag_without_legacy_executor_or_duplicate_effect(self):
        rt = self.runtime(True)
        before = rt.bank.balance_cents
        with patch.object(rt, '_run_linkage_node', side_effect=AssertionError('legacy disabled')):
            task_id = self.create(rt)
            self.assertEqual(rt.bank.balance_cents, before - 100000)
            self.assertEqual(len(rt.bank.subaccount_locks), 1)
            old = rt.get_linkage_task(task_id)
            dag = rt.dag_store.get(linkage_to_dag(old).dag_id)
            self.assertEqual(dag.metadata['execution_owner'], 'dag')
            self.assertIsNone(rt.audit.get_linkage_task(task_id))
            rt.restore_scheduled_tasks(now=rt.get_linkage_task(task_id)['nodes'][1]['due_at'].replace('00:00:00', '12:00:00'))
            rt.restore_scheduled_tasks(now=rt.get_linkage_task(task_id)['nodes'][1]['due_at'].replace('00:00:00', '12:00:00'))
            self.assertEqual(len(rt.bank.linkage_orders), 1)
            synced = rt.dag_store.get(dag.dag_id)
            self.assertEqual({n.node_id: n.status for n in synced.nodes}['B'], 'succeeded')
            self.assertTrue(rt.cancel_linkage_task(task_id)['ok'])
            self.assertEqual(rt.bank.balance_cents, before)
            self.assertEqual(rt.dag_store.get(dag.dag_id).status, 'cancelled')
            self.assertTrue(rt.audit.verify_trace(dag.trace_id)['valid'])

    def test_toggle_off_never_hands_adopted_dag_back_to_legacy_executor(self):
        first = self.runtime(True)
        task_id = self.create(first)
        dag_id = linkage_to_dag(first.get_linkage_task(task_id)).dag_id
        stale = first.dag_store.get(dag_id).to_dict()
        second = self.runtime(False)
        self.assertFalse(second.cancel_linkage_task(task_id)['ok'])
        self.assertEqual(first.dag_store.get(dag_id).to_dict(), stale)
        third = self.runtime(True)
        self.assertEqual(third.dag_store.get(dag_id).status, 'running')
        self.assertEqual(third.bank.locked_subaccount_cents, 100000)
        self.assertTrue(third.cancel_linkage_task(task_id)['ok'])
        self.assertEqual(third.bank.locked_subaccount_cents, 0)
        self.assertEqual(third.bank.linkage_orders, [])
        self.assertTrue(third.audit.verify_trace(stale['trace_id'])['valid'])

    def test_startup_migrates_preexisting_terminal_tasks_without_executing_them(self):
        rt = self.runtime(False)
        task = legacy_task(state='CANCELLED', statuses=('COMPENSATED', 'CANCELLED', 'CANCELLED'))
        rt.audit.save_linkage_task(task)
        with patch.object(ba.MockBankCore, 'lock_subaccount', side_effect=AssertionError('terminal must not run')):
            restored = self.runtime(True)
        self.assertEqual(restored.dag_store.get(linkage_to_dag(task).dag_id).status, 'cancelled')
        self.assertEqual(restored.audit.get_linkage_task(task['task_id']), task)


if __name__ == '__main__':
    unittest.main()
