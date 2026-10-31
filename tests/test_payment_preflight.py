"""E1 阶段 1：支付预检必须是只读事实报告，不授予执行权限。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import banking_agent as ba  # noqa: E402
from preflight import build_payment_preflight  # noqa: E402


class PaymentPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = ba.AgentRuntime(db_path=Path(self.tmp.name) / "bank.sqlite3", offline=True)

    def tearDown(self):
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        self.runtime.audit.db.close()
        self.tmp.cleanup()

    def _report(self, amount_cents, beneficiary="妈妈", **kwargs):
        return build_payment_preflight(
            self.runtime.bank,
            ba.POLICY,
            amount_cents=amount_cents,
            beneficiary=beneficiary,
            actor_id="demo-user",
            **kwargs,
        )

    def test_report_has_three_state_dimensions_and_budget_is_computed(self):
        report = self._report(20_000, requires_confirmation=True)
        checks = report["checks"]
        self.assertEqual(checks["available_funds"]["status"], "satisfied")
        self.assertEqual(checks["daily_limit"]["status"], "satisfied")
        self.assertEqual(checks["beneficiary"]["status"], "satisfied")
        self.assertEqual(checks["card_status"]["status"], "satisfied")
        self.assertEqual(checks["authentication"]["status"], "missing")
        self.assertEqual(checks["budget"]["status"], "satisfied")
        self.assertTrue(report["read_only"])
        self.assertIn("执行成功", report["disclaimer"])
        self.assertTrue(all(item["status"] in {"satisfied", "missing", "unknown"} for item in checks.values()))

    def test_balance_and_daily_limit_boundaries_are_exact(self):
        balance = self.runtime.bank.available_balance("demo-user")
        funds_boundary = self._report(balance)
        self.assertEqual(funds_boundary["checks"]["available_funds"]["status"], "satisfied")
        funds_over = self._report(balance + 1)
        self.assertEqual(funds_over["checks"]["available_funds"]["status"], "missing")

        limit = ba.POLICY["daily_transfer_limit_cents"]
        limit_boundary = self._report(limit)
        self.assertEqual(limit_boundary["checks"]["daily_limit"]["status"], "satisfied")
        limit_over = self._report(limit + 1)
        self.assertEqual(limit_over["checks"]["daily_limit"]["status"], "missing")

    def test_new_recipient_card_states_and_unknowns_are_distinguished(self):
        self.assertEqual(self._report(10_000, beneficiary="妈妈")["checks"]["beneficiary"]["status"], "satisfied")
        self.assertEqual(self._report(10_000, beneficiary="女儿")["checks"]["beneficiary"]["status"], "missing")
        self.assertEqual(self._report(10_000, beneficiary=None)["checks"]["beneficiary"]["status"], "unknown")

        self.runtime.bank.cards[0]["status"] = "受限"
        self.assertEqual(self._report(10_000)["checks"]["card_status"]["status"], "missing")
        self.runtime.bank.cards.clear()
        self.assertEqual(self._report(10_000)["checks"]["card_status"]["status"], "unknown")

    def test_strong_auth_requirement_is_reported_not_granted(self):
        report = self._report(10_000, requires_confirmation=True, requires_strong_auth=True)
        self.assertEqual(report["checks"]["authentication"]["status"], "missing")
        self.assertEqual(report["checks"]["authentication"]["evidence"]["required"], "strong_auth")

    def test_preflight_does_not_change_account_or_core_state(self):
        before_balance = self.runtime.bank.balance().data
        before_daily = self.runtime.bank.daily_transfer_total_cents()
        before_transactions = json.dumps(self.runtime.bank.transactions, ensure_ascii=False, sort_keys=True)
        before_subscriptions = json.dumps(self.runtime.bank.subscriptions, ensure_ascii=False, sort_keys=True)
        before_core_rows = self.runtime.bank.db.execute(
            "SELECT transaction_id,status,debit_applied FROM core_transactions ORDER BY transaction_id"
        ).fetchall()

        self._report(50_000)

        self.assertEqual(self.runtime.bank.balance().data, before_balance)
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents(), before_daily)
        self.assertEqual(json.dumps(self.runtime.bank.transactions, ensure_ascii=False, sort_keys=True), before_transactions)
        self.assertEqual(json.dumps(self.runtime.bank.subscriptions, ensure_ascii=False, sort_keys=True), before_subscriptions)
        self.assertEqual(
            self.runtime.bank.db.execute(
                "SELECT transaction_id,status,debit_applied FROM core_transactions ORDER BY transaction_id"
            ).fetchall(),
            before_core_rows,
        )

    def test_chat_attaches_report_to_plan_and_audits_it(self):
        response = self.runtime.chat("给妈妈转 200 元")
        self.assertIn("preflight", response["plan"])
        self.assertIn("preflight", response["plan_view"])
        self.assertIn("可用资金", " ".join(response["plan_view"]["preflight"]["messages"]))
        row = self.runtime.audit.db.execute(
            "SELECT event_type,payload_json FROM audit_events WHERE trace_id=? AND event_type='payment.preflight'",
            (response["trace_id"],),
        ).fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(json.loads(row[1])["preflight"]["read_only"], True)
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents(), 0)


if __name__ == "__main__":
    unittest.main()
