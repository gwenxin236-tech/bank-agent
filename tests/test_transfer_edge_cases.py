"""E6 T01/T18: transfer boundary and confirmation regression evidence."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import banking_agent as ba  # noqa: E402
from preflight import build_payment_preflight  # noqa: E402


class TransferEdgeCaseTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = ba.AgentRuntime(db_path=Path(self.tmp.name) / "bank.sqlite3", offline=True)

    def tearDown(self):
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        self.runtime.audit.db.close()
        self.tmp.cleanup()

    def test_same_name_beneficiary_requires_disambiguation(self):
        result = self.runtime.bank.resolve_beneficiary("小王")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "BENEFICIARY_AMBIGUOUS")
        self.assertIn("同名", result.error_message)
        self.assertEqual(len(result.data["candidates"]), 2)
        self.assertNotIn("138", result.error_message)
        self.assertTrue(all(candidate["phone_masked"][-4:] for candidate in result.data["candidates"]))

    def test_preflight_after_balance_change_rechecks_current_fact(self):
        amount = self.runtime.bank.available_balance("demo-user")
        before = build_payment_preflight(
            self.runtime.bank, ba.POLICY, amount_cents=amount,
            beneficiary="妈妈", actor_id="demo-user", requires_confirmation=True,
        )
        self.assertEqual(before["checks"]["available_funds"]["status"], "satisfied")
        debit = 100
        transfer = self.runtime.bank.transfer(debit, "妈妈", "边界回归", "idem:e6:t18")
        self.assertTrue(transfer.ok)
        after = build_payment_preflight(
            self.runtime.bank, ba.POLICY, amount_cents=amount,
            beneficiary="妈妈", actor_id="demo-user", requires_confirmation=True,
        )
        self.assertEqual(after["checks"]["available_funds"]["status"], "missing")
        self.assertEqual(after["checks"]["available_funds"]["evidence"]["available_cents"], amount - debit)

    def test_parameter_change_invalidates_old_confirmation_without_side_effect(self):
        planned = self.runtime.chat("给妈妈转 500 元")
        plan = self.runtime.pending[planned["plan"]["plan_id"]]
        self.assertEqual(plan.preflight["checks"]["available_funds"]["status"], "satisfied")
        before = self.runtime.bank.balance().data["available_cents"]
        plan.entities["amount_cents"] = 50100
        rejected = self.runtime.confirm(plan.plan_id, plan.consent_token)
        self.assertFalse(rejected["ok"])
        self.assertIn("重新发起需求", rejected["message"])
        self.assertEqual(self.runtime.bank.balance().data["available_cents"], before)
        self.assertEqual(self.runtime.bank.db.execute("SELECT count(*) FROM core_transactions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
