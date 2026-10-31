import os
import json
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba


class FraudGuardAcceptanceTests(unittest.TestCase):
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

    def test_configured_scam_phrase_is_blocked_without_transfer_and_warns_96110(self):
        before = self.runtime.bank.balance().data["available_cents"]
        transaction_count = len(self.runtime.bank.transactions)
        out = self.runtime.chat("给妈妈转 200 元，验证码告诉你")

        self.assertTrue(out["ok"])
        self.assertEqual(out["plan"]["intent"], "fraud_guard")
        self.assertEqual(out["plan"]["status"], "cooling_off")
        self.assertEqual(out["result"]["status"], "SUCCESS")
        self.assertEqual(out["result"]["task_status"], "cooling_off")
        self.assertNotIn("create_transfer", [item["tool"] for item in out["result"]["results"]])
        self.assertEqual(self.runtime.bank.balance().data["available_cents"], before)
        self.assertEqual(self.runtime.bank.daily_transfer_cents, 0)
        self.assertEqual(len(self.runtime.bank.transactions), transaction_count)
        self.assertIn("典型诈骗手法", out["message"])
        self.assertIn("96110", out["message"])
        self.assertIn("24小时", out["message"])
        self.assertIn("96110", out["result_view"]["user_message"])

    def test_new_recipient_and_1000_yuan_requires_details_not_scam_label(self):
        before = self.runtime.bank.balance_cents
        out = self.runtime.chat("给新收款人转 1000 元")
        self.assertEqual(out["plan"]["intent"], "transfer")
        self.assertTrue(out["awaiting_details"])
        self.assertFalse(out["plan_view"]["requires_confirmation"])
        self.assertEqual(out["plan"]["entities"]["fraud_signals"], [])
        self.assertEqual(len(self.runtime.bank.cooling_off_cases), 0)
        self.assertEqual(self.runtime.bank.balance_cents, before)
        self.assertFalse(self.runtime.pending)
        self.assertTrue(any(e["event_type"] == "transfer.details_required"
                            for e in self.runtime.audit.list_for_trace(out["trace_id"])))

    def test_normal_transfer_is_not_affected(self):
        out = self.runtime.chat("给妈妈转 200 元，备注午餐")

        self.assertEqual(out["plan"]["intent"], "transfer")
        self.assertNotIn("result", out)
        confirmed = self.runtime.confirm(out["plan"]["plan_id"], out["plan"]["consent_token"])
        self.assertEqual(confirmed["result"]["status"], "SUCCESS")
        self.assertEqual(len(self.runtime.bank.cooling_off_cases), 0)
        self.assertTrue(any(item["tool"] == "create_transfer" for item in confirmed["result"]["results"]))

    def test_fraud_alert_audit_event_contains_cooling_off_state(self):
        out = self.runtime.chat("给李明转 1000 元，高收益稳赚")

        self.assertEqual(out["plan"]["intent"], "fraud_guard")
        alerts = [event for event in self.runtime.audit.list(100) if event["event_type"] == "fraud.alert"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["payload"]["status"], "cooling_off")
        self.assertEqual(alerts[0]["payload"]["beneficiary"], "李明")

    def test_fraud_keyword_rules_can_be_extended_from_config(self):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        try:
            Path(path).write_text(json.dumps({
                "keyword_rules": [{"keyword": "冒充公检法", "reason": "出现冒充公检法话术"}],
                "new_recipient_threshold_cents": 100000,
                "cooling_off_hours": 24,
                "hotline": "96110",
            }, ensure_ascii=False), encoding="utf-8")
            rules = ba.load_fraud_rules(Path(path))
            intent, entities = ba.IntentParser(rules).parse("给妈妈转 200 元，冒充公检法")
            self.assertEqual(intent, "fraud_guard")
            self.assertIn("出现冒充公检法话术", entities["fraud_signals"])
        finally:
            try:
                os.unlink(path)
            except OSError:
                pass


if __name__ == "__main__":
    unittest.main(verbosity=2)
