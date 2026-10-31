"""E2 phase 2: partial DAG outcomes remain visible in user receipts."""
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))

import banking_agent as ba
from task_dag import DagNode, TaskDag
from transaction_views import dag_receipt, receipt_view


class PartialReceiptTests(unittest.TestCase):
    def make_dag(self, nodes, status="failed"):
        return TaskDag(
            dag_id="receipt-dag",
            plan_id="receipt-plan",
            intent="multi_step",
            trace_id="receipt-trace",
            actor_id="alice",
            nodes=nodes,
            status=status,
        )

    def test_failed_dag_keeps_completed_failed_and_unexecuted_steps_separate(self):
        completed = DagNode(
            "pay",
            tool="create_transfer",
            status="succeeded",
            result={"execution": {"ok": True, "status": "succeeded"}},
        )
        failed = DagNode(
            "notify",
            tool="notify_guardian",
            status="failed",
            result={"execution": {"ok": False, "status": "failed"}},
        )
        skipped = DagNode("later", tool="order_cake", status="skipped")
        compensated = DagNode(
            "reserve",
            tool="lock_subaccount",
            status="compensated",
            compensation={"tool": "compensate_subaccount_lock", "params": {}},
            result={
                "execution": {"ok": True, "status": "succeeded"},
                "compensation": {
                    "status": "succeeded",
                    "result": {"ok": True, "data": {"tool": "compensate_subaccount_lock"}},
                },
            },
        )
        receipt = dag_receipt(self.make_dag([completed, failed, skipped, compensated]))

        self.assertEqual(receipt["status"], "PARTIAL")
        by_id = {item["node_id"]: item for item in receipt["items"]}
        self.assertEqual(by_id["pay"]["status"], "COMPLETED")
        self.assertEqual(by_id["notify"]["status"], "FAILED")
        self.assertEqual(by_id["later"]["status"], "NOT_EXECUTED")
        self.assertEqual(by_id["reserve"]["status"], "COMPLETED")
        self.assertEqual(len(receipt["compensations"]), 1)
        self.assertEqual(receipt["compensations"][0]["status"], "COMPENSATED")
        self.assertEqual(receipt["compensations"][0]["tool"], "compensate_subaccount_lock")

    def test_running_action_is_presented_as_executed_pending_verification(self):
        running = DagNode(
            "pay",
            tool="create_transfer",
            status="running",
            result={},
        )
        receipt = dag_receipt(self.make_dag([running], status="waiting_human"))

        self.assertEqual(receipt["status"], "UNKNOWN")
        self.assertEqual(receipt["items"][0]["status"], "PENDING_VERIFICATION")
        self.assertEqual(receipt["items"][0]["status_label"], "已执行待核验")

    def test_receipt_view_is_compatible_with_result_view_and_does_not_overclaim(self):
        receipt = {
            "status": "PARTIAL",
            "items": [
                {"node_id": "pay", "label": "转账", "status": "COMPLETED", "status_label": "已完成", "detail": ""},
                {"node_id": "later", "label": "下单鲜花", "status": "NOT_EXECUTED", "status_label": "未执行", "detail": ""},
            ],
            "compensations": [],
        }
        view = receipt_view(receipt, "trace-receipt")
        self.assertFalse(view["ok"])
        self.assertEqual(view["status"], "PARTIAL")
        self.assertIn("部分完成", view["user_message"])
        self.assertNotIn("全部完成", view["user_message"])
        self.assertEqual(view["receipt"]["trace_id"], "trace-receipt")

        plan = SimpleNamespace(trace_id="trace-receipt")
        presented = ba.present_result(plan, {"ok": False, "status": "FAILED", "receipt": receipt})
        self.assertEqual(presented["status"], "PARTIAL")
        self.assertEqual(presented["receipt"]["items"][0]["status"], "COMPLETED")

    def test_frontend_has_explicit_partial_and_unknown_receipt_rendering(self):
        script = (Path(__file__).resolve().parents[1] / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("function renderReceipt(receipt)", script)
        self.assertIn("逐项办理回执", script)
        self.assertIn("PARTIAL:'部分完成'", script)
        self.assertIn("UNKNOWN:'结果核验中'", script)


if __name__ == "__main__":
    unittest.main()
