"""E3: deterministic double-entry views and bounded account coverage."""
import importlib
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from ledger_views import apply_category_correction, build_ledger_views


class LedgerViewsTests(unittest.TestCase):
    def test_repayment_is_cash_outflow_but_not_consumption(self):
        views = build_ledger_views([
            {"id": "repay-1", "date": "2026-09-10", "merchant": "信用卡还款", "category": "信用卡还款", "amount_cents": -100000, "transaction_type": "credit_card_repayment"},
        ])
        self.assertEqual(views["consumer_view"]["total_cents"], 0)
        self.assertEqual(views["cash_flow_view"]["outflow_cents"], 100000)
        self.assertIn("包含还款，不等于消费", views["cash_flow_view"]["label"])

    def test_cross_source_consumption_is_deduplicated(self):
        views = build_ledger_views([
            {"id": "card-1", "date": "2026-09-10", "merchant": "超市", "category": "购物", "amount_cents": -100000, "source": "credit_card", "canonical_id": "purchase-1"},
            {"id": "import-1", "date": "2026-09-10", "merchant": "超市", "category": "购物", "amount_cents": -100000, "source": "imported_bill", "canonical_id": "purchase-1"},
        ])
        self.assertEqual(views["consumer_view"]["total_cents"], 100000)
        self.assertEqual(len(views["consumer_view"]["items"]), 1)
        self.assertEqual(views["consumer_view"]["items"][0]["canonical_id"], "purchase-1")

    def test_partial_refund_reduces_consumption(self):
        views = build_ledger_views([
            {"id": "purchase-1", "date": "2026-09-10", "merchant": "家电", "category": "购物", "amount_cents": -100000},
            {"id": "refund-1", "date": "2026-09-12", "merchant": "家电", "category": "退款", "amount_cents": 25000, "transaction_type": "refund", "refund_for": "purchase-1"},
        ])
        self.assertEqual(views["consumer_view"]["total_cents"], 75000)
        self.assertEqual(views["consumer_view"]["refund_cents"], 25000)

    def test_period_uses_posted_date_and_keeps_refund_in_its_month(self):
        views = build_ledger_views([
            {"id": "aug", "date": "2026-08-31", "posted_date": "2026-08-31", "category": "购物", "amount_cents": -10000},
            {"id": "sep-refund", "date": "2026-09-02", "posted_date": "2026-09-02", "category": "退款", "amount_cents": 3000, "transaction_type": "refund", "refund_for": "aug"},
        ], period_start="2026-09-01", period_end="2026-09-30")
        self.assertEqual(views["consumer_view"]["total_cents"], 0)
        self.assertEqual(views["consumer_view"]["refund_cents"], 3000)
        self.assertEqual([item["id"] for item in views["consumer_view"]["items"]], ["sep-refund"])

    def test_processing_is_separate_from_completed_totals(self):
        views = build_ledger_views([
            {"id": "processing-1", "date": "2026-09-10", "category": "购物", "amount_cents": -50000, "status": "processing"},
        ])
        self.assertEqual(views["consumer_view"]["total_cents"], 0)
        self.assertEqual(views["cash_flow_view"]["outflow_cents"], 0)
        self.assertEqual(views["funds_status"]["processing_cents"], 50000)

    def test_coverage_is_explicit_for_single_account(self):
        views = build_ledger_views([], coverage={"scope": "demo checking account", "complete": False, "updated_at": "2026-09-28T00:00:00+00:00"})
        self.assertFalse(views["coverage"]["complete"])
        self.assertIn("当前仅覆盖", views["coverage"]["message"])

    def test_category_correction_keeps_original_record(self):
        original = {"id": "tx-1", "category": "其他", "amount_cents": -1000}
        corrected = apply_category_correction(original, "餐饮", corrected_by="user")
        self.assertEqual(corrected["category"], "其他")
        self.assertEqual(corrected["corrected_category"], "餐饮")
        self.assertEqual(corrected["correction_record"]["original_category"], "其他")

    def test_agent_main_chain_exposes_two_views_and_scope(self):
        ba = importlib.import_module("banking_agent")
        with tempfile.TemporaryDirectory() as directory:
            runtime = ba.AgentRuntime(db_path=Path(directory) / "ledger.sqlite3", offline=True)
            try:
                output = runtime.chat("这个月我到底花了多少钱")
                self.assertEqual(output["plan"]["intent"], "financial_health")
                result = output.get("result", {})
                detail = result["steps"][0]["detail"]
                import json
                data = json.loads(detail)
                self.assertIn("consumer_view", data)
                self.assertIn("cash_flow_view", data)
                self.assertIn("funds_status", data)
                self.assertFalse(data["coverage"]["complete"])
                self.assertIn("当前仅覆盖", output["result_view"]["user_message"])
            finally:
                runtime.recovery_scheduler.stop()
                runtime.bank.close()
                runtime.audit.db.close()


if __name__ == "__main__":
    unittest.main()
