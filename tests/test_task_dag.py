"""D6 phase 1: isolated executor tests; no bank/HTTP entry point is switched."""
import copy
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from task_dag import DagNode, TaskDag, DagExecutor


class TaskDagTests(unittest.TestCase):
    def node(self, name, deps=(), compensation=False, **kwargs):
        return DagNode(node_id=name, tool=name, depends_on=list(deps),
                       compensation={"tool": "undo_" + name, "params": {}} if compensation else None,
                       **kwargs)

    def dag(self, *nodes):
        return TaskDag(dag_id="dag-1", plan_id="plan-1", intent="test",
                       trace_id="trace-1", actor_id="alice", nodes=list(nodes))

    def executor(self, dag, execute=None, clock=None, on_change=None):
        self.calls = []
        self.events = []
        self.snapshots = []
        def runner(node, tool, params, key):
            self.calls.append((node.node_id, tool, copy.deepcopy(params), key))
            if execute:
                return execute(node, tool, params, key)
            return {"ok": True, "data": {"tool": tool}}
        def checkpoint(snapshot, event):
            self.snapshots.append(copy.deepcopy(snapshot))
            self.events.append(copy.deepcopy(event))
            if on_change:
                on_change(snapshot, event)
        return DagExecutor(dag, runner, on_change=checkpoint, clock=clock)

    def test_node_state_machine_all_pairs_and_dependency_guard(self):
        allowed = {
            "pending": {"ready", "skipped"}, "ready": {"running", "skipped"},
            "running": {"succeeded", "failed"}, "succeeded": {"compensated"},
            "failed": set(), "skipped": set(), "compensated": set(),
        }
        for source in allowed:
            for target in allowed:
                with self.subTest(source=source, target=target):
                    node = self.node("a", status=source)
                    if target in allowed[source]:
                        node.transition(target, {})
                        self.assertEqual(node.status, target)
                    else:
                        with self.assertRaises(ValueError):
                            node.transition(target, {})
                        self.assertEqual(node.status, source)
        for state in ("pending", "ready", "running", "failed", "skipped", "compensated", None):
            node = self.node("b", ["a"])
            with self.subTest(dependency=state), self.assertRaises(ValueError):
                node.transition("ready", {"a": state})
            self.assertEqual(node.status, "pending")
        node.transition("ready", {"a": "succeeded"})
        self.assertEqual(node.status, "ready")

    def test_task_state_machine_all_pairs(self):
        allowed = {
            "running": {"paused", "waiting_human", "failed", "compensating", "completed"},
            "paused": {"running", "compensating"},
            "waiting_human": {"running", "handoff"},
            "compensating": {"cancelled"},
            "failed": set(), "completed": set(), "cancelled": set(), "handoff": set(),
        }
        for source in allowed:
            for target in allowed:
                with self.subTest(source=source, target=target):
                    dag = self.dag(self.node("a"))
                    dag.status = source
                    if target in allowed[source]:
                        dag.transition(target)
                        self.assertEqual(dag.status, target)
                    else:
                        with self.assertRaises(ValueError):
                            dag.transition(target)
                        self.assertEqual(dag.status, source)

    def test_topological_order_handles_unordered_diamond(self):
        dag = self.dag(self.node("d", ["b", "c"]), self.node("b", ["a"]),
                       self.node("c", ["a"]), self.node("a"))
        executor = self.executor(dag)
        executor.run()
        self.assertEqual([x[1] for x in self.calls], ["a", "b", "c", "d"])
        self.assertEqual(dag.status, "completed")
        self.assertTrue(all(n.status == "succeeded" for n in dag.nodes))

    def test_cycles_unknown_duplicate_edges_and_duplicate_ids_rejected(self):
        cases = [
            [self.node("a", ["a"])],
            [self.node("a", ["b"]), self.node("b", ["a"])],
            [self.node("root"), self.node("a", ["b"]), self.node("b", ["a"])],
            [self.node("a", ["missing"])],
            [self.node("a"), self.node("a")],
            [self.node("a"), self.node("b", ["a", "a"])],
        ]
        for nodes in cases:
            with self.subTest(nodes=[n.node_id for n in nodes]), self.assertRaises(ValueError):
                self.dag(*nodes)

    def test_invalid_types_and_statuses_rejected(self):
        for kwargs in ({"node_type": "parallel"}, {"status": "made_up"},
                       {"compensation": {"tool": "undo", "params": [], "extra": True}}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                DagNode("a", tool="a", **kwargs)
        for identifier in ("", "a:b"):
            with self.subTest(identifier=identifier), self.assertRaises(ValueError):
                self.dag(self.node(identifier))
        with self.assertRaises(ValueError):
            self.dag()
        with self.assertRaises(ValueError):
            DagNode("gate", node_type="human_gate", tool="create_transfer")
        with self.assertRaises(ValueError):
            DagNode("delay", node_type="delay", params={"run_at": "2026-09-22T00:00:00"})

    def test_money_rejects_float_boolean_string_and_nested_fraction(self):
        for params in ({"amount_cents": 1.1}, {"amount_cents": True},
                       {"amount_cents": "100"}, {"amount": 1.1},
                       {"nested": [{"budget_cents": 0.5}]}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                self.node("a", params=params)
        with self.assertRaises(ValueError):
            DagNode("a", tool="a", compensation={"tool": "undo", "params": {"amount_cents": 1.5}})
        dag = self.dag(self.node("a", params={"amount_cents": 12345, "confirmed": False}))
        self.executor(dag).run()
        self.assertEqual(self.calls[0][2]["amount_cents"], 12345)
        self.assertIs(type(self.calls[0][2]["amount_cents"]), int)

    def test_fixed_idempotency_keys_and_restored_success_never_reexecute(self):
        dag = self.dag(self.node("a"), self.node("b", ["a"]))
        executor = self.executor(dag)
        executor.run()
        self.assertEqual([c[3] for c in self.calls], ["idem:dag-1:a", "idem:dag-1:b"])
        executor.run()
        self.assertEqual(len(self.calls), 2)
        restored = TaskDag.from_dict(json.loads(json.dumps(dag.to_dict())))
        self.executor(restored).run()
        self.assertEqual(self.calls, [])
        self.assertEqual(restored.to_dict(), dag.to_dict())
        with self.assertRaises(ValueError):
            self.dag(self.node("a", idempotency_key="forged"))

    def test_resume_after_checkpoint_skips_committed_success(self):
        dag = self.dag(self.node("a"), self.node("b", ["a"]))
        executor = None
        def runner(node, *_):
            if node.node_id == "a":
                executor.pause()
            return {"ok": True, "data": {"amount_cents": 100}}
        executor = self.executor(dag, runner)
        executor.run()
        self.assertEqual(dag.status, "paused")
        self.assertEqual([n.status for n in dag.nodes], ["succeeded", "pending"])
        recovered = TaskDag.from_dict(dag.to_dict())
        executor = self.executor(recovered)
        executor.run()
        self.assertEqual(self.calls, [])
        executor.resume()
        executor.run()
        self.assertEqual([c[1] for c in self.calls], ["b"])
        self.assertEqual(recovered.status, "completed")

    def test_failure_is_fail_fast_and_compensates_reverse_topology(self):
        dag = self.dag(self.node("d", ["c"]), self.node("c", ["b"]),
                       self.node("b", ["a"], True), self.node("a", compensation=True))
        def runner(node, tool, *_):
            return {"ok": tool != "c", "data": {"tool": tool}}
        self.executor(dag, runner).run()
        self.assertEqual([c[1] for c in self.calls], ["a", "b", "c", "undo_b", "undo_a"])
        self.assertEqual(dag.status, "failed")
        self.assertEqual({n.node_id: n.status for n in dag.nodes},
                         {"a": "compensated", "b": "compensated", "c": "failed", "d": "skipped"})
        self.assertEqual([c[3] for c in self.calls[-2:]],
                         ["idem:dag-1:b:compensate", "idem:dag-1:a:compensate"])
        self.assertEqual(dag.nodes[-1].result["execution"], {"ok": True, "data": {"tool": "a"}})

    def test_failure_skips_nodes_without_compensation(self):
        dag = self.dag(self.node("a"), self.node("b", ["a"]))
        self.executor(dag, lambda n, *_: {"ok": n.node_id == "a"}).run()
        self.assertEqual([c[1] for c in self.calls], ["a", "b"])
        self.assertEqual(dag.nodes[0].status, "succeeded")
        self.assertEqual(dag.status, "failed")

    def test_cancel_from_running_or_paused_compensates_once(self):
        for paused in (False, True):
            with self.subTest(paused=paused):
                dag = self.dag(self.node("a", compensation=True),
                               DagNode("later", node_type="delay", params={"run_at": "2099-01-01T00:00:00Z"}, depends_on=["a"]))
                executor = self.executor(dag)
                executor.run()
                if paused:
                    executor.pause()
                executor.cancel()
                self.assertEqual(dag.status, "cancelled")
                self.assertEqual([n.status for n in dag.nodes], ["compensated", "skipped"])
                self.assertEqual([c[1] for c in self.calls], ["a", "undo_a"])
                with self.assertRaises(ValueError):
                    executor.cancel()
                executor.run()
                self.assertEqual(len(self.calls), 2)

    def test_failed_compensation_does_not_claim_cancelled_or_retry(self):
        dag = self.dag(self.node("a", compensation=True),
                       DagNode("later", node_type="delay", params={"run_at": "2099-01-01T00:00:00Z"}, depends_on=["a"]))
        executor = self.executor(dag, lambda n, tool, *_: {"ok": tool != "undo_a"})
        executor.run()
        executor.cancel()
        self.assertEqual(dag.status, "compensating")
        self.assertEqual(dag.nodes[0].status, "succeeded")
        self.assertEqual(dag.nodes[0].result["compensation"]["status"], "failed")
        executor.run()
        with self.assertRaises(ValueError):
            executor.cancel()
        self.assertEqual([c[1] for c in self.calls], ["a", "undo_a"])
        self.assertTrue(any(e["event_type"] == "dag.compensation.failed" for e in self.events))

    def test_delay_waits_until_due_but_does_not_block_independent_branch(self):
        now = datetime(2026, 9, 22, 0, 0, tzinfo=timezone.utc)
        dag = self.dag(DagNode("delay", node_type="delay", params={"run_at": "2026-09-23T00:00:00Z"}),
                       self.node("later", ["delay"]), self.node("independent"))
        executor = self.executor(dag, clock=lambda: now)
        executor.run()
        self.assertEqual([c[1] for c in self.calls], ["independent"])
        self.assertEqual(dag.status, "running")
        self.assertEqual(dag.nodes[0].status, "ready")
        now = datetime(2026, 9, 23, 0, 0, tzinfo=timezone.utc)
        executor.run()
        self.assertEqual([c[1] for c in self.calls], ["independent", "later"])
        self.assertEqual(dag.status, "completed")

    def test_human_gate_stops_until_explicit_approval(self):
        dag = self.dag(DagNode("confirm", node_type="human_gate"), self.node("pay", ["confirm"]))
        executor = self.executor(dag)
        executor.run()
        self.assertEqual(dag.status, "waiting_human")
        self.assertEqual(self.calls, [])
        executor.run()
        with self.assertRaises(ValueError):
            executor.resume()
        with self.assertRaises(ValueError):
            executor.approve("pay")
        self.assertEqual(self.calls, [])
        executor.approve("confirm")
        self.assertEqual(dag.status, "running")
        self.assertEqual(self.calls, [])  # Approval itself never submits a bank operation.
        executor.run()
        self.assertEqual([c[1] for c in self.calls], ["pay"])
        self.assertEqual(dag.status, "completed")
        with self.assertRaises(ValueError):
            executor.approve("confirm")

    def test_human_handoff_and_rejection_do_not_execute_downstream(self):
        for handoff in (False, True):
            with self.subTest(handoff=handoff):
                dag = self.dag(DagNode("confirm", node_type="human_gate"), self.node("pay", ["confirm"]))
                executor = self.executor(dag)
                executor.run()
                if handoff:
                    executor.handoff()
                    self.assertEqual(dag.status, "handoff")
                else:
                    executor.approve("confirm", approved=False)
                    self.assertEqual(dag.status, "failed")
                    self.assertEqual(dag.nodes[1].status, "skipped")
                executor.run()
                self.assertEqual(self.calls, [])

    def test_recovered_running_action_is_not_retried_or_human_approved(self):
        dag = self.dag(self.node("pay", status="running"), self.node("later", ["pay"]))
        executor = self.executor(TaskDag.from_dict(dag.to_dict()))
        executor.run()
        self.assertEqual(self.calls, [])
        self.assertEqual(executor.dag.status, "waiting_human")
        with self.assertRaises(ValueError):
            executor.approve("pay")
        self.assertEqual(executor.dag.nodes[0].status, "running")
        self.assertTrue(any(e["event_type"] == "dag.reconciliation.required" for e in self.events))

    def test_tool_timeout_malformed_result_and_processing_never_mean_success(self):
        def timeout(*_):
            raise TimeoutError("could have debited")
        runners = [timeout, lambda *_: None, lambda *_: {"ok": "yes"},
                   lambda *_: {"ok": True, "data": {"status": "processing", "transaction_id": "tx1"}}]
        for runner in runners:
            with self.subTest(runner=runner):
                dag = self.dag(self.node("pay"), self.node("later", ["pay"]))
                executor = self.executor(dag, runner)
                executor.run()
                self.assertEqual(dag.status, "waiting_human")
                self.assertEqual([n.status for n in dag.nodes], ["running", "pending"])
                executor.run()
                self.assertEqual(len(self.calls), 1)
                self.assertNotIn("could have debited", json.dumps(self.events))

    def test_failed_transaction_status_cannot_be_overridden_by_ok_flag(self):
        dag = self.dag(self.node("pay"))
        self.executor(dag, lambda *_: {"ok": True, "data": {"status": "failed"}}).run()
        self.assertEqual(dag.status, "failed")
        self.assertEqual(dag.nodes[0].status, "failed")

    def test_each_transition_checkpoints_identity_and_no_sensitive_params(self):
        dag = self.dag(self.node("pay", params={"recipient": "13800138000", "amount_cents": 100}))
        def runner(*_):
            self.assertEqual(self.snapshots[-1]["nodes"][0]["status"], "running")
            return {"ok": True, "data": {"transaction_id": "tx1"}}
        self.executor(dag, runner).run()
        transitions = [e for e in self.events if e["event_type"] in ("dag.node.transition", "dag.transition")]
        self.assertEqual([(e["from"], e["to"]) for e in transitions],
                         [("pending", "ready"), ("ready", "running"), ("running", "succeeded"), ("running", "completed")])
        for event in self.events:
            self.assertEqual(event["actor_id"], "alice")
            self.assertEqual(event["trace_id"], "trace-1")
            self.assertEqual(event["dag_id"], "dag-1")
            self.assertEqual(event["plan_id"], "plan-1")
        self.assertNotIn("13800138000", json.dumps(self.events))
        self.assertEqual(self.snapshots[-1], dag.to_dict())

    def test_checkpoint_failure_stops_before_tool_and_poisoned_executor_cannot_continue(self):
        dag = self.dag(self.node("pay"))
        def broken_store(snapshot, event):
            if event.get("to") == "running":
                raise OSError("disk full")
        executor = self.executor(dag, on_change=broken_store)
        with self.assertRaises(OSError):
            executor.run()
        self.assertEqual(self.calls, [])
        with self.assertRaises(RuntimeError):
            executor.run()
        self.assertEqual(self.calls, [])

    def test_results_and_callback_arguments_are_snapshots_not_live_mutable_state(self):
        answer = {"ok": True, "data": {"amount_cents": 100}}
        dag = self.dag(self.node("pay", params={"amount_cents": 100}))
        def runner(node, tool, params, key):
            params["amount_cents"] = 999
            node.params["amount_cents"] = 999
            return answer
        def mutate(snapshot, event):
            snapshot["actor_id"] = "mallory"
            event["actor_id"] = "mallory"
        self.executor(dag, runner, on_change=mutate).run()
        answer["data"]["amount_cents"] = 999
        self.assertEqual(dag.actor_id, "alice")
        self.assertEqual(dag.nodes[0].params["amount_cents"], 100)
        self.assertEqual(dag.nodes[0].result["execution"]["data"]["amount_cents"], 100)

    def test_restored_ready_node_cannot_bypass_dependency_check(self):
        dag = self.dag(self.node("a"), self.node("pay", ["a"], status="ready"))
        dag.nodes[0].status = "skipped"
        executor = self.executor(dag)
        with self.assertRaises(ValueError):
            executor.run()
        self.assertEqual(self.calls, [])
        self.assertEqual(dag.nodes[1].status, "ready")

    def test_invalid_result_status_never_claims_success(self):
        for status in ([], {}, 123, "unrecognized", "PROCESSING"):
            with self.subTest(status=status):
                dag = self.dag(self.node("pay"))
                self.executor(dag, lambda *_: {"ok": True, "data": {"status": status}}).run()
                self.assertEqual(dag.status, "waiting_human")
                self.assertEqual(dag.nodes[0].status, "running")
                self.assertEqual(len(self.calls), 1)

    def test_compensation_failure_does_not_undo_its_predecessor_or_retry_after_restore(self):
        dag = self.dag(self.node("a", compensation=True), self.node("b", ["a"], True),
                       self.node("fail", ["b"]))
        def runner(node, tool, *_):
            return {"ok": tool not in {"fail", "undo_b"}}
        self.executor(dag, runner).run()
        self.assertEqual([c[1] for c in self.calls], ["a", "b", "fail", "undo_b"])
        self.assertEqual(dag.status, "failed")
        self.assertEqual(dag.nodes[0].status, "succeeded")
        restored = TaskDag.from_dict(dag.to_dict())
        self.executor(restored).run()
        self.assertEqual(self.calls, [])

    def test_restart_during_compensation_never_resubmits_in_flight_compensation(self):
        dag = self.dag(self.node("a", compensation=True, status="succeeded"),
                       self.node("b", ["a"], True, status="succeeded"),
                       self.node("fail", ["b"], status="failed"))
        dag.nodes[1].result = {"compensation": {"status": "running"}}
        self.executor(TaskDag.from_dict(dag.to_dict())).run()
        self.assertEqual(self.calls, [])
        self.assertEqual(dag.nodes[0].status, "succeeded")

    def test_checkpoint_failure_inside_callback_propagates_without_success(self):
        dag = self.dag(self.node("pay"))
        executor = None
        def runner(*_):
            executor.pause()
            return {"ok": True}
        def checkpoint(snapshot, event):
            if event.get("to") == "paused":
                raise OSError("cannot save pause")
        executor = self.executor(dag, runner, on_change=checkpoint)
        with self.assertRaises(OSError):
            executor.run()
        self.assertEqual(dag.nodes[0].status, "running")
        with self.assertRaises(RuntimeError):
            executor.run()
        self.assertEqual(len(self.calls), 1)

    def test_recovered_failure_blocks_independent_action_and_compensates(self):
        dag = self.dag(self.node("a", compensation=True, status="succeeded"),
                       self.node("b", ["a"], status="failed"), self.node("independent"))
        restored = TaskDag.from_dict(dag.to_dict())
        self.executor(restored).run()
        self.assertEqual([c[1] for c in self.calls], ["undo_a"])
        self.assertEqual(restored.status, "failed")
        self.assertEqual(restored.nodes[2].status, "skipped")

    def test_reentrant_scheduler_and_cancel_during_dispatch_cannot_run_second_tool(self):
        dag = self.dag(self.node("pay"), self.node("later", ["pay"]))
        executor = None
        def runner(*_):
            with self.assertRaises(RuntimeError):
                executor.run()
            with self.assertRaises(ValueError):
                executor.cancel()
            executor.pause()
            return {"ok": True}
        executor = self.executor(dag, runner)
        executor.run()
        self.assertEqual([c[1] for c in self.calls], ["pay"])
        self.assertEqual(dag.status, "paused")
        self.assertEqual(dag.nodes[1].status, "pending")


if __name__ == "__main__":
    unittest.main()
