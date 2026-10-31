"""E7-A: card loss/unloss is a real, strongly authenticated MockBankCore write."""
from pathlib import Path
import json
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import banking_agent as ba
import production_contracts as pc


class CardLossTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = ba.AgentRuntime(db_path=Path(self.tmp.name) / "card.sqlite3", offline=True)

    def tearDown(self):
        if hasattr(self.runtime, "recovery_scheduler"):
            self.runtime.recovery_scheduler.stop()
        self.runtime.audit.db.close()
        self.runtime.bank.close()
        self.tmp.cleanup()

    def _plan(self, text):
        response = self.runtime.chat(text)
        self.assertTrue(response["ok"], response)
        self.assertEqual(response["plan"]["intent"], "card_manage")
        self.assertEqual(response["plan"]["risk_level"], "red")
        self.assertTrue(response["plan"]["requires_strong_auth"])
        return response

    def _complete(self, response):
        plan = response["plan"]
        challenge = self.runtime.confirm(plan["plan_id"], plan["consent_token"])
        self.assertTrue(challenge.get("needs_auth"), challenge)
        verified = self.runtime.verify(plan["plan_id"], plan["consent_token"], "123456",
                                       credential_id=challenge["auth_challenge"]["credential_id"])
        self.assertTrue(verified["ok"], verified)
        completed = self.runtime.confirm(plan["plan_id"], plan["consent_token"],
                                         credential_id=verified["credential_id"])
        return completed

    def test_adapter_contract_changes_card_state_and_is_idempotent(self):
        cards = [{"id": "card_001", "status": "正常", "last4": "6222"}]
        adapter = pc.MockCardManagementAdapter(cards)
        ctx = pc.AdapterContext(trace_id="trace-card", actor_id="demo-user", idempotency_key="idem-card-loss")
        first = adapter.report_loss(ctx, "card_001")
        self.assertTrue(first.ok)
        self.assertEqual(first.data["status"], "reported_loss")
        replay = adapter.report_loss(ctx, "card_001")
        self.assertTrue(replay.ok)
        self.assertTrue(replay.data["idempotent_replay"])
        self.assertEqual(cards[0]["status"], "reported_loss")
        queried = adapter.query_card(ctx, "card_001")
        self.assertEqual(queried.data["status"], "reported_loss")

    def test_loss_requires_strong_auth_and_then_changes_real_state(self):
        before = self.runtime.bank.cards[0]["status"]
        response = self._plan("我的卡丢了，帮我挂失")
        self.assertEqual(response["plan"]["entities"]["card_operation"], "report_loss")
        self.assertEqual(before, "正常")
        challenge = self.runtime.confirm(response["plan"]["plan_id"], response["plan"]["consent_token"])
        self.assertTrue(challenge["needs_auth"])
        self.assertEqual(self.runtime.bank.cards[0]["status"], "正常")
        completed = self._complete(response)
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(self.runtime.bank.cards[0]["status"], "reported_loss")
        self.assertIn("已挂失", completed["message"])
        self.assertEqual(completed["result"]["status"], "SUCCESS")
        self.assertEqual(completed["result"]["results"][-1]["data"]["status"], "reported_loss")

    def test_repeated_loss_is_idempotent_and_audited(self):
        response = self._plan("我的卡丢了帮我挂失")
        completed = self._complete(response)
        self.assertTrue(completed["ok"], completed)
        replay = self.runtime.bank.report_card_loss("card_001", "demo-user", "idem:replay")
        replay_again = self.runtime.bank.report_card_loss("card_001", "demo-user", "idem:replay")
        self.assertTrue(replay.ok)
        self.assertTrue(replay_again.ok)
        self.assertTrue(replay_again.data["idempotent_replay"])
        events = self.runtime.audit.list_for_trace(response["trace_id"])
        self.assertTrue(any(e["event_type"] == "card.loss.completed" for e in events))
        self.assertTrue(any(e["event_type"] == "card.status.verified" for e in events))

    def test_loss_makes_payment_preflight_card_dimension_missing_without_debit(self):
        response = self._plan("我的卡丢了帮我挂失")
        self.assertTrue(self._complete(response)["ok"])
        before = self.runtime.bank.balance().data["available_cents"]
        report = ba.build_payment_preflight(self.runtime.bank, ba.POLICY, amount_cents=10000, beneficiary="妈妈")
        self.assertEqual(report["checks"]["card_status"]["status"], "missing")
        self.assertIn("不能", report["checks"]["card_status"]["message"])
        self.assertEqual(self.runtime.bank.balance().data["available_cents"], before)

    def test_unloss_is_red_and_restores_normal_after_verification(self):
        self.runtime.bank.report_card_loss("card_001", "demo-user", "idem:setup-loss")
        response = self._plan("帮我解挂银行卡")
        self.assertEqual(response["plan"]["entities"]["card_operation"], "unreport_loss")
        challenge = self.runtime.confirm(response["plan"]["plan_id"], response["plan"]["consent_token"])
        self.assertTrue(challenge["needs_auth"])
        self.assertEqual(self.runtime.bank.cards[0]["status"], "reported_loss")
        completed = self._complete(response)
        self.assertTrue(completed["ok"], completed)
        self.assertEqual(self.runtime.bank.cards[0]["status"], "正常")
        self.assertIn("解挂", completed["message"])

    def test_unknown_card_fails_without_changing_other_cards(self):
        before = json.loads(json.dumps(self.runtime.bank.cards, ensure_ascii=False))
        result = self.runtime.bank.report_card_loss("missing-card", "demo-user", "idem:missing")
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code if hasattr(result, "error_code") else result.code, "CARD_NOT_FOUND")
        self.assertEqual(self.runtime.bank.cards, before)

    def test_readiness_declares_mock_card_write_boundary(self):
        report = self.runtime.readiness_report()
        control = next(item for item in report["controls"] if item["id"] == "card_management_write")
        self.assertEqual(control["status"], "blocked")
        self.assertIn("Mock", control["evidence"])
        self.assertIn("真实核心", control["evidence"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
