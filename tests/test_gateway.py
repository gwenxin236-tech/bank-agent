import hashlib
import hmac
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba
from channel_contracts import AgentGateway, UnifiedMessage, verify_hmac_signature
from security_controls import SlidingWindowRateLimiter


class GatewayContractTests(unittest.TestCase):
    def setUp(self):
        self.old_db = ba.DB_PATH
        fd, path = tempfile.mkstemp(suffix=".sqlite3")
        os.close(fd)
        ba.DB_PATH = Path(path)
        self.runtime = ba.AgentRuntime()
        self.gateway = AgentGateway(self.runtime)

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

    def test_web_message_uses_common_contract_and_session(self):
        msg = UnifiedMessage(channel="web", user_id="u-1", conversation_id="c-1", text="查余额")
        result = self.gateway.handle(msg)
        self.assertTrue(result["ok"])
        self.assertEqual(result["gateway"]["channel"], "web")
        self.assertTrue(result["gateway"]["session_id"].startswith("sess_"))

    def test_non_text_is_rejected_without_touching_agent(self):
        msg = UnifiedMessage(channel="feishu", user_id="u-1", conversation_id="c-1", message_type="image")
        result = self.gateway.handle(msg)
        self.assertFalse(result["ok"])
        self.assertIn("文字消息", result["message"])

    def test_webhook_signature(self):
        secret = "test-secret"
        body = b'{"event":"message"}'
        digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertTrue(verify_hmac_signature(secret, body, "sha256=" + digest))
        self.assertFalse(verify_hmac_signature(secret, body + b"x", "sha256=" + digest))

    def test_rate_limiter_has_bounded_window(self):
        limiter = SlidingWindowRateLimiter(limit=2, window_seconds=60)
        self.assertTrue(limiter.allow("client"))
        self.assertTrue(limiter.allow("client"))
        self.assertFalse(limiter.allow("client"))
        self.assertTrue(limiter.allow("other-client"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
