"""E6 T11: subscription cancellation failure must remain pending/unknown."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import banking_agent as ba  # noqa: E402


class SubscriptionFailureTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = ba.AgentRuntime(db_path=Path(self.tmp.name) / "bank.sqlite3", offline=True)

    def tearDown(self):
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        self.runtime.audit.db.close()
        self.tmp.cleanup()

    def test_timeout_is_unknown_and_not_success(self):
        original = [dict(subscription) for subscription in self.runtime.bank.subscriptions]
        self.runtime.bank.inject_fault("transport_timeout")
        planned = self.runtime.chat("取消某视频会员，退款到账了吗")
        completed = self.runtime.confirm(planned["plan"]["plan_id"], planned["plan"]["consent_token"])
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["result"]["status"], "UNKNOWN")
        self.assertEqual(completed["result_view"]["status"], "UNKNOWN")
        self.assertIn("待处理", completed["result_view"]["user_message"])
        self.assertNotIn("已取消", completed["result_view"]["user_message"])
        self.assertEqual(self.runtime.bank.subscriptions, original)
        self.assertTrue(any(
            event["event_type"] == "subscription.unknown"
            for event in self.runtime.audit.list_for_trace(planned["trace_id"])
        ))

    def test_unreachable_is_unknown_and_does_not_retry(self):
        self.runtime.bank.inject_fault("network_unreachable")
        planned = self.runtime.chat("取消某视频会员")
        first = self.runtime.confirm(planned["plan"]["plan_id"], planned["plan"]["consent_token"])
        self.assertEqual(first["result"]["status"], "UNKNOWN")
        self.runtime.bank.inject_fault(None)
        second = self.runtime.confirm(planned["plan"]["plan_id"], planned["plan"]["consent_token"])
        self.assertFalse(second["ok"])
        self.assertNotEqual(second.get("result", {}).get("status"), "SUCCESS")


if __name__ == "__main__":
    unittest.main(verbosity=2)
