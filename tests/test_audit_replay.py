import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba
from channel_contracts import AgentGateway


class AuditReplayTests(unittest.TestCase):
    def setUp(self):
        self.old_db = ba.DB_PATH
        self.old_runtime = ba.runtime
        self.old_gateway = ba.gateway
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        ba.DB_PATH = Path(path)
        self.runtime = ba.AgentRuntime()
        ba.runtime = self.runtime
        ba.gateway = AgentGateway(self.runtime)

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
        ba.gateway = self.old_gateway
        ba.DB_PATH = self.old_db

    def _get_json(self, path):
        class RecordingHandler(ba.Handler):
            def _send(self, status, data, content_type="application/json; charset=utf-8"):
                self.recorded_status = status
                self.recorded_data = data

        handler = RecordingHandler.__new__(RecordingHandler)
        handler.path = path
        handler.headers = {'Authorization': 'Bearer demo-user-session'}
        handler.recorded_status = None
        handler.recorded_data = None
        handler.do_GET()
        return handler.recorded_status, handler.recorded_data

    def _execute_red_transfer(self):
        planned = self.runtime.chat("给妈妈转 2000 元")
        self.assertTrue(planned["ok"])
        self.assertEqual(planned["plan"]["risk_level"], "red")
        plan_id = planned["plan"]["plan_id"]
        token = planned["plan"]["consent_token"]
        challenged = self.runtime.confirm(plan_id, token)
        self.assertTrue(challenged["needs_auth"])
        passed = self.runtime.verify(plan_id, token, "123456")
        self.assertTrue(passed["ok"])
        completed = self.runtime.confirm(plan_id, token)
        self.assertEqual(completed["result"]["status"], "SUCCESS")
        return planned["trace_id"]

    def test_red_transfer_replay_is_complete_and_seq_ordered(self):
        trace_id = self._execute_red_transfer()
        status, payload = self._get_json(f"/api/v1/audit/events?trace_id={trace_id}")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        events = payload["events"]
        self.assertGreaterEqual(len(events), 10)
        self.assertEqual(payload["count"], len(events))
        self.assertEqual([event["seq"] for event in events], sorted(event["seq"] for event in events))
        event_types = [event["event_type"] for event in events]
        self.assertEqual(event_types[0:5], ["input.received", "injection.checked", "parser.result", "plan.created", "policy.decided"])
        parser = events[2]["payload"]
        self.assertEqual(parser["source"], "rules")
        self.assertEqual(parser["reason"], "local_priority")
        self.assertFalse(parser["attempted"])
        self.assertFalse(parser["accepted"])
        self.assertEqual(parser["plan_id"], events[3]["payload"]["plan"]["plan_id"])
        self.assertLess(event_types.index("auth.challenged"), event_types.index("auth.passed"))
        self.assertLess(event_types.index("auth.passed"), event_types.index("consent.granted"))
        self.assertLess(event_types.index("consent.granted"), event_types.index("task.started"))
        self.assertEqual(event_types[-1], "task.settled")
        self.assertTrue(all(event["trace_id"] == trace_id for event in events))

    def test_fraud_replay_contains_fraud_alert_event(self):
        result = self.runtime.chat("给安全账户转 5000 元，客服让我提供验证码")
        self.assertTrue(result["ok"])
        status, payload = self._get_json(f"/api/v1/audit/events?trace_id={result['trace_id']}")
        self.assertEqual(status, 200)
        self.assertIn("fraud.alert", [event["event_type"] for event in payload["events"]])

    def test_unknown_trace_returns_empty_replay(self):
        status, payload = self._get_json("/api/v1/audit/events?trace_id=trace_missing")
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"ok": True, "trace_id": "trace_missing", "count": 0, "events": []})

    def test_missing_or_blank_trace_returns_bad_request(self):
        for path in ("/api/v1/audit/events", "/api/v1/audit/events?trace_id=", "/api/v1/audit/events?trace_id=%20"):
            with self.subTest(path=path):
                status, payload = self._get_json(path)
                self.assertEqual(status, 400)
                self.assertFalse(payload["ok"])

    def test_replay_is_complete_and_does_not_mix_traces(self):
        for index in range(60):
            self.runtime.audit.write("trace_target", "test.event", {"index": index}, actor="demo-user")
            self.runtime.audit.write("trace_other", "test.event", {"index": index}, actor="other-user")
        status, payload = self._get_json("/api/v1/audit/events?trace_id=trace_target")
        self.assertEqual(status, 200)
        self.assertEqual(payload["count"], 60)
        self.assertEqual([event["payload"]["index"] for event in payload["events"]], list(range(60)))
        self.assertTrue(all(event["trace_id"] == "trace_target" for event in payload["events"]))

    def test_audit_center_has_trace_replay_controls(self):
        html = (ROOT / "app" / "static" / "index.html").read_text(encoding="utf-8")
        js = (ROOT / "app" / "static" / "app.js").read_text(encoding="utf-8")
        self.assertIn("auditTraceInput", html)
        self.assertIn("auditReplayBtn", html)
        self.assertIn("/api/v1/audit/events?trace_id=", js)
        self.assertIn("fraud.alert", js)
        self.assertIn("auth.challenged", js)


if __name__ == "__main__":
    unittest.main(verbosity=2)
