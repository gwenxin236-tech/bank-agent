import os
import tempfile
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba


class DailyTransferPolicyTests(unittest.TestCase):
    def setUp(self):
        self.old_db = ba.DB_PATH
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        ba.DB_PATH = Path(path)
        self.runtime = ba.AgentRuntime()

    def tearDown(self):
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        try:
            self.runtime.audit.db.close()
        except Exception:
            pass
        try:
            os.unlink(ba.DB_PATH)
        except OSError:
            pass
        ba.DB_PATH = self.old_db

    def _confirm(self, text, actor_id="demo-user"):
        planned = self.runtime.chat(text, actor_id=actor_id)
        self.assertEqual(planned["plan"]["risk_level"], "yellow")
        return self.runtime.confirm(planned["plan"]["plan_id"], planned["plan"]["consent_token"])

    def test_settled_daily_total_promotes_next_transfer_to_red(self):
        first = self._confirm("给妈妈转 600 元")
        self.assertEqual(first["result"]["status"], "SUCCESS")
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents("demo-user"), 60000)

        second = self.runtime.chat("给妈妈转 500 元")
        self.assertEqual(second["plan"]["risk_level"], "red")
        self.assertTrue(second["plan"]["requires_strong_auth"])

    def test_boundary_values_999_1000_yellow_and_1001_red(self):
        self.assertEqual(self.runtime.chat("给妈妈转 999 元")["plan"]["risk_level"], "yellow")
        self.assertEqual(self.runtime.chat("给妈妈转 1000 元")["plan"]["risk_level"], "yellow")
        self.assertEqual(self.runtime.chat("给妈妈转 1001 元")["plan"]["risk_level"], "red")

    def test_daily_total_is_scoped_to_actor(self):
        self._confirm("给妈妈转 900 元", actor_id="alice")
        other = self.runtime.chat("给妈妈转 200 元", actor_id="bob")
        self.assertEqual(other["plan"]["risk_level"], "yellow")
        alice_next = self.runtime.chat("给妈妈转 200 元", actor_id="alice")
        self.assertEqual(alice_next["plan"]["risk_level"], "red")
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents("alice"), 90000)
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents("bob"), 0)

    def test_zero_and_negative_amount_are_rejected_without_debit(self):
        before_balance = self.runtime.bank.balance().data["available_cents"]
        for text in ("给妈妈转 0 元", "给妈妈转 -1 元"):
            out = self.runtime.chat(text)
            self.assertTrue(out["blocked"])
            self.assertEqual(self.runtime.bank.balance().data["available_cents"], before_balance)
            self.assertEqual(self.runtime.bank.daily_transfer_total_cents("demo-user"), 0)

    def test_confirm_double_click_executes_one_transfer_and_one_daily_total(self):
        planned = self.runtime.chat("给妈妈转 600 元")
        plan_id, token = planned["plan"]["plan_id"], planned["plan"]["consent_token"]
        results = []
        barrier = threading.Barrier(2)

        def click():
            barrier.wait()
            results.append(self.runtime.confirm(plan_id, token))

        threads = [threading.Thread(target=click) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        successes = [item for item in results if item.get("result", {}).get("status") == "SUCCESS"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents("demo-user"), 60000)

    def test_cancel_after_execution_does_not_rewind_settled_daily_total(self):
        planned = self.runtime.chat("给妈妈转 600 元")
        plan_id, token = planned["plan"]["plan_id"], planned["plan"]["consent_token"]
        executed = self.runtime.confirm(plan_id, token)
        self.assertEqual(executed["result"]["status"], "SUCCESS")
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents("demo-user"), 60000)

        cancelled = self.runtime.cancel(plan_id, token)
        self.assertFalse(cancelled["ok"])
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents("demo-user"), 60000)


if __name__ == "__main__":
    unittest.main(verbosity=2)
