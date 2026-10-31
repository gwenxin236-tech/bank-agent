import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba


class C5AuditChainTests(unittest.TestCase):
    def setUp(self):
        self.old_db = ba.DB_PATH
        self.old_runtime = ba.runtime
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        ba.DB_PATH = Path(path)
        self.runtime = ba.AgentRuntime()
        self.runtime.family.demo_sessions['c5-session'] = {'user_id': 'user-c5', 'device_id': 'device-c5'}
        ba.runtime = self.runtime

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
        ba.runtime = self.old_runtime
        ba.DB_PATH = self.old_db

    def _get_json(self, path):
        class RecordingHandler(ba.Handler):
            def _send(self, status, data, content_type="application/json; charset=utf-8"):
                self.recorded_status = status
                self.recorded_data = data

        handler = RecordingHandler.__new__(RecordingHandler)
        handler.path = path
        handler.headers = {'Authorization': 'Bearer c5-session'}
        handler.recorded_status = None
        handler.recorded_data = None
        handler.do_GET()
        return handler.recorded_status, handler.recorded_data

    def _red_trace(self):
        planned = self.runtime.chat("给妈妈转 2000 元", actor_id="user-c5", device_id="device-c5")
        plan_id = planned["plan"]["plan_id"]
        token = planned["plan"]["consent_token"]
        self.assertTrue(self.runtime.confirm(plan_id, token)["needs_auth"])
        self.assertTrue(self.runtime.verify(plan_id, token, "123456")["ok"])
        self.assertEqual(self.runtime.confirm(plan_id, token)["result"]["status"], "SUCCESS")
        return planned["trace_id"]

    def test_normal_chain_verify_endpoint_and_complete_event_schema(self):
        trace_id = self._red_trace()
        status, result = self._get_json(f"/api/v1/audit/verify?trace_id={trace_id}")
        self.assertEqual(status, 200)
        self.assertTrue(result["ok"])
        self.assertTrue(result["valid"])
        self.assertIsNone(result["break_at"])

        events = self.runtime.audit.list_for_trace(trace_id)
        self.assertGreater(len(events), 5)
        required = {"trace_id", "task_id", "user_id", "device_id", "event_type", "operation",
                    "risk_level", "decision_reason", "tool_name", "timestamp", "prev_hash", "current_hash"}
        for event in events:
            self.assertTrue(required.issubset(event), event)
            self.assertEqual(event["timestamp"], event["created_at"])
            self.assertTrue(event["current_hash"])
        self.assertEqual(events[0]["prev_hash"], "GENESIS")
        for previous, current in zip(events, events[1:]):
            # The global chain may contain other traces between these events.
            row = self.runtime.audit.db.execute(
                "SELECT current_hash, event_hash FROM audit_events WHERE seq = ?", (current["seq"] - 1,)
            ).fetchone()
            if row:
                self.assertEqual(current["prev_hash"], row[0] or row[1])

    def test_tamper_is_located_published_and_freezes_high_risk_user(self):
        trace_id = self._red_trace()
        row = self.runtime.audit.db.execute(
            "SELECT seq, payload_json FROM audit_events WHERE trace_id = ? ORDER BY seq LIMIT 1", (trace_id,)
        ).fetchone()
        payload = json.loads(row[1])
        payload["tampered"] = True
        self.runtime.audit.db.execute(
            "UPDATE audit_events SET payload_json = ? WHERE seq = ?",
            (json.dumps(payload, ensure_ascii=False), row[0]),
        )
        self.runtime.audit.db.commit()

        status, result = self._get_json(f"/api/v1/audit/verify?trace_id={trace_id}")
        self.assertEqual(status, 200)
        self.assertFalse(result["valid"])
        self.assertEqual(result["break_at"], row[0])
        self.assertEqual(result["reason"], "current_hash_mismatch")
        self.assertEqual(result["security_event"], "security.tamper")
        self.assertIn("user-c5", result["frozen_users"])
        self.assertTrue(self.runtime.is_user_frozen("user-c5"))

        blocked = self.runtime.chat("给妈妈转 2000 元", actor_id="user-c5", device_id="device-c5")
        self.assertFalse(blocked["ok"])
        self.assertTrue(blocked["blocked"])
        self.assertIn("人工解锁", blocked["message"])
        self.runtime.unlock_user("user-c5")
        self.assertFalse(self.runtime.is_user_frozen("user-c5"))

        event_types = [e["event_type"] for e in self.runtime.audit.list_for_trace(trace_id)]
        self.assertIn("security.tamper", event_types)

    def test_sensitive_values_are_masked_in_audit_storage_and_output(self):
        trace_id = "trace-sensitive"
        self.runtime.audit.write(trace_id, "sensitive.test", {
            "phone": "13812345678",
            "card": "6222021234567890",
            "identity": "110101199001011234",
            "otp": "123456",
            "nested": ["13987654321", "6217000012345678"],
        }, actor="user-c5", risk_level="red", intent="transfer")
        events = self.runtime.audit.list_for_trace(trace_id)
        serialized = json.dumps(events, ensure_ascii=False)
        for secret in ("13812345678", "6222021234567890", "110101199001011234", "123456", "13987654321", "6217000012345678"):
            self.assertNotIn(secret, serialized)
        self.assertIn("138****5678", serialized)
        self.assertIn("622*********7890", serialized)
        self.assertIn("******", serialized)

        status, output = self._get_json(f"/api/v1/audit/events?trace_id={trace_id}")
        self.assertEqual(status, 200)
        self.assertNotIn("13812345678", json.dumps(output, ensure_ascii=False))

    def test_tampered_identity_field_still_freezes_consistent_trace_user(self):
        trace_id = self._red_trace()
        row = self.runtime.audit.db.execute(
            "SELECT seq FROM audit_events WHERE trace_id = ? ORDER BY seq LIMIT 1", (trace_id,)
        ).fetchone()
        self.runtime.audit.db.execute(
            "UPDATE audit_events SET user_id = ? WHERE seq = ?", ("attacker", row[0])
        )
        self.runtime.audit.db.commit()

        result = self.runtime.verify_audit_trace(trace_id)
        self.assertFalse(result["valid"])
        self.assertEqual(result["reason"], "current_hash_mismatch")
        self.assertIn("user-c5", result["frozen_users"])
        self.assertNotIn("attacker", result["frozen_users"])

    def test_missing_trace_is_rejected_for_verify(self):
        status, result = self._get_json("/api/v1/audit/verify")
        self.assertEqual(status, 400)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
