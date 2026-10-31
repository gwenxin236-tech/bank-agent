"""E1 阶段 2：预算约束只读计算与保护性阻断。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import banking_agent as ba  # noqa: E402
from preflight import build_payment_preflight  # noqa: E402


class BudgetConstraintTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = ba.AgentRuntime(db_path=Path(self.tmp.name) / "bank.sqlite3", offline=True)

    def tearDown(self):
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        self.runtime.audit.db.close()
        self.tmp.cleanup()

    def _report(self, amount_cents):
        return build_payment_preflight(
            self.runtime.bank,
            ba.POLICY,
            amount_cents=amount_cents,
            beneficiary="妈妈",
            actor_id="demo-user",
            requires_confirmation=True,
        )

    def test_disposable_budget_and_shortfall_are_exact(self):
        self.runtime.bank.confirmed_commitments = [
            {"id": "rent", "name": "本月房租", "amount_cents": 800_000, "status": "confirmed"},
            {"id": "card", "name": "信用卡还款", "amount_cents": 300_000, "status": "confirmed"},
        ]
        report = self._report(100_000)
        budget = report["checks"]["budget"]
        expected = self.runtime.bank.available_balance() - 1_100_000 - ba.POLICY["preflight_security_buffer_cents"]
        self.assertEqual(budget["status"], "satisfied")
        self.assertEqual(budget["evidence"]["available_cents"], self.runtime.bank.available_balance())
        self.assertEqual(budget["evidence"]["confirmed_expenses_cents"], 1_104_300)
        self.assertEqual(budget["evidence"]["disposable_cents"], expected - 4_300)
        self.assertEqual(budget["evidence"]["projected_disposable_cents"], expected - 4_300 - 100_000)
        self.assertEqual(budget["evidence"]["shortfall_cents"], 0)

        self.runtime.bank.confirmed_commitments = [
            {"id": "rent", "name": "本月房租", "amount_cents": 2_600_000, "status": "confirmed"},
            {"id": "card", "name": "信用卡还款", "amount_cents": 1_000_000, "status": "confirmed"},
        ]
        amount = self.runtime.bank.available_balance() - 3_704_300 + 1
        report = self._report(amount)
        budget = report["checks"]["budget"]
        self.assertEqual(budget["status"], "missing")
        self.assertEqual(budget["evidence"]["shortfall_cents"], 1)
        self.assertIn("预算缺口", budget["message"])
        self.assertEqual(report["budget_options"], ["adjust_amount", "adjust_budget", "defer", "human_handoff"])

    def test_projected_income_is_not_counted_as_disposable(self):
        self.runtime.bank.confirmed_commitments = [
            {"id": "rent", "name": "本月房租", "amount_cents": 2_600_000, "status": "confirmed"},
        ]
        before = self.runtime.bank.available_balance()
        report = self._report(before - 2_600_000 - ba.POLICY["preflight_security_buffer_cents"] + 1)
        budget = report["checks"]["budget"]
        self.assertEqual(budget["status"], "missing")
        self.assertEqual(budget["evidence"]["projected_income_cents"], 0)
        self.assertNotIn(3_200_000, budget["evidence"].values())

    def test_budget_preflight_is_read_only_and_recomputed_after_amount_change(self):
        self.runtime.bank.confirmed_commitments = [
            {"id": "rent", "name": "本月房租", "amount_cents": 2_600_000, "status": "confirmed"},
            {"id": "card", "name": "信用卡还款", "amount_cents": 1_000_000, "status": "confirmed"},
        ]
        before_balance = self.runtime.bank.balance().data
        before_transactions = json.dumps(self.runtime.bank.transactions, ensure_ascii=False, sort_keys=True)
        report_large = self._report(24_000_000)
        report_small = self._report(100_000)
        self.assertEqual(report_large["checks"]["budget"]["status"], "missing")
        self.assertEqual(report_small["checks"]["budget"]["status"], "satisfied")
        self.assertEqual(self.runtime.bank.balance().data, before_balance)
        self.assertEqual(json.dumps(self.runtime.bank.transactions, ensure_ascii=False, sort_keys=True), before_transactions)

    def test_chat_budget_conflict_requires_user_choice_and_does_not_queue_or_debit(self):
        self.runtime.bank.confirmed_commitments = [
            {"id": "rent", "name": "本月房租", "amount_cents": 2_600_000, "status": "confirmed"},
            {"id": "card", "name": "信用卡还款", "amount_cents": 1_000_000, "status": "confirmed"},
        ]
        before_balance = self.runtime.bank.available_balance()
        response = self.runtime.chat("给妈妈转 240000 元")
        self.assertFalse(response["ok"])
        self.assertEqual(response["plan"]["preflight"]["checks"]["budget"]["status"], "missing")
        self.assertFalse(response["plan_view"]["requires_confirmation"])
        self.assertEqual(response["budget_options"], ["adjust_amount", "adjust_budget", "defer", "human_handoff"])
        self.assertNotIn(response["plan"]["plan_id"], self.runtime.pending)
        self.assertEqual(self.runtime.bank.available_balance(), before_balance)
        audit = self.runtime.audit.db.execute(
            "SELECT 1 FROM audit_events WHERE trace_id=? AND event_type='budget.constraint.blocked'",
            (response["trace_id"],),
        ).fetchone()
        self.assertIsNotNone(audit)


if __name__ == "__main__":
    unittest.main()
