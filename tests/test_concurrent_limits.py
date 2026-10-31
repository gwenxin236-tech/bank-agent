"""E6 T04: concurrent daily-limit regression evidence."""
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import banking_agent as ba  # noqa: E402


class ConcurrentLimitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = ba.AgentRuntime(db_path=Path(self.tmp.name) / "bank.sqlite3", offline=True)

    def tearDown(self):
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        self.runtime.audit.db.close()
        self.tmp.cleanup()

    def test_two_requests_cannot_jointly_cross_daily_limit(self):
        limit = ba.POLICY["daily_transfer_limit_cents"]
        amount = limit // 2 + 1
        before = self.runtime.bank.balance().data["available_cents"]
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda key: self.runtime.bank.transfer(amount, "妈妈", "并发额度回归", key),
                ("idem:e6:concurrent:a", "idem:e6:concurrent:b"),
            ))
        self.assertEqual(sum(result.ok for result in results), 1)
        self.assertEqual(sum(result.data.get("amount_cents", 0) for result in results if result.ok), amount)
        self.assertEqual(self.runtime.bank.daily_transfer_total_cents(), amount)
        self.assertEqual(self.runtime.bank.balance().data["available_cents"], before - amount)
        self.assertTrue(any(result.error_code == "DAILY_LIMIT" for result in results if not result.ok))
        self.assertLessEqual(self.runtime.bank.daily_transfer_total_cents(), limit)


if __name__ == "__main__":
    unittest.main(verbosity=2)
