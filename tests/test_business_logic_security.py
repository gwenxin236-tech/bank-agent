import copy
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import tempfile
import threading
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba
from channel_contracts import AgentGateway, UnifiedMessage


class RuntimeFixture(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rt = ba.AgentRuntime(db_path=Path(self.tmp.name) / "business.sqlite3", offline=True)
        self.gateway = AgentGateway(self.rt)

    def tearDown(self):
        self.rt.recovery_scheduler.stop()
        self.rt.bank.close()
        self.rt.audit.db.close()
        self.tmp.cleanup()

    def chat(self, text):
        return self.gateway.handle(UnifiedMessage("web", "demo-user", "d3-business", text=text))

    def rows(self):
        return [dict(r) for r in self.rt.bank.db.execute("SELECT * FROM core_transactions ORDER BY transaction_id")]

    def assert_event(self, trace, kind, **payload):
        events = [e for e in self.rt.audit.list_for_trace(trace) if e["event_type"] == kind]
        self.assertTrue(events, (trace, kind))
        self.assertTrue(any(all(e["payload"].get(k) == v for k, v in payload.items()) for e in events), events)
        self.assertTrue(self.rt.audit.verify_trace(trace)["valid"])


class BusinessLogicSecurityTests(RuntimeFixture):

    def test_negative_amount_is_blocked_without_debit(self):
        before = self.rt.bank.balance().data["available_cents"]
        out = self.chat("给妈妈转 -500 元")
        self.assertTrue(out["blocked"])
        self.assertEqual(self.rt.bank.balance().data["available_cents"], before)
        self.assertNotIn(out["plan"]["plan_id"], self.rt.pending)
        self.assertEqual(self.rows(), [])
        self.assert_event(out["trace_id"], "business_input.blocked")

    def test_fractional_cent_is_blocked_without_debit(self):
        before = self.rt.bank.balance().data["available_cents"]
        out = self.chat("给妈妈转 0.001 元")
        self.assertTrue(out["blocked"])
        self.assertIn("最多支持到分", out["message"])
        self.assertEqual(self.rt.bank.balance().data["available_cents"], before)
        self.assertNotIn(out["plan"]["plan_id"], self.rt.pending)
        self.assertEqual(self.rows(), [])
        self.assert_event(out["trace_id"], "business_input.blocked")

    def test_confirm_is_one_time_under_double_click(self):
        out = self.chat("给妈妈转 200 元")
        pid, token = out["plan"]["plan_id"], out["plan"]["consent_token"]
        before = self.rt.bank.balance().data["available_cents"]
        barrier = threading.Barrier(2)

        def click():
            barrier.wait(timeout=5)
            return self.rt.confirm(pid, token)

        # Futures propagate worker exceptions instead of silently losing a result.
        with ThreadPoolExecutor(2) as pool:
            futures = [pool.submit(click) for _ in range(2)]
            results = [f.result(timeout=10) for f in futures]
        self.assertEqual(len(results), 2)
        successes = [x for x in results if x.get("result", {}).get("status") == "SUCCESS"]
        self.assertEqual(len(successes), 1)
        self.assertEqual(sum(not x["ok"] for x in results), 1)
        self.assertEqual(self.rt.bank.balance().data["available_cents"], before - 20000)
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.rows()[0]["status"], "succeeded")
        self.assertEqual(self.rows()[0]["debit_applied"], 1)
        self.assertNotIn(pid, self.rt.pending)
        self.assert_event(out["trace_id"], "consent.replay")
        self.assert_event(out["trace_id"], "transaction.status.queried", status="succeeded")

    def test_old_token_cannot_execute_another_plan(self):
        first = self.chat("给妈妈转 200 元")
        second = self.chat("给妈妈转 300 元")
        before = self.rt.bank.balance().data["available_cents"]
        pending = copy.deepcopy(self.rt.pending)
        rejected = self.rt.confirm(second["plan"]["plan_id"], first["plan"]["consent_token"])
        self.assertFalse(rejected["ok"])
        self.assertIn("凭据无效", rejected["message"])
        self.assertEqual(self.rt.bank.balance().data["available_cents"], before)
        self.assertEqual(self.rt.pending, pending)
        self.assertEqual(self.rows(), [])
        self.assert_event(second["trace_id"], "consent.rejected", reason="invalid_action_token")

    def test_toc_tou_rechecks_balance_before_side_effect(self):
        out = self.chat("给妈妈转 200 元")
        original = self.rt.bank.check_transfer_constraints
        calls = {"count": 0}
        def race_check(amount):
            calls["count"] += 1
            if calls["count"] == 1:
                return original(amount)
            self.rt.bank.balance_cents = 1000
            return ba.ToolResult(False, "check_transfer_constraints", error_code="INSUFFICIENT_FUNDS", error_message="可用余额不足。")
        self.rt.bank.check_transfer_constraints = race_check
        result = self.rt.confirm(out["plan"]["plan_id"], out["plan"]["consent_token"])
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertEqual(self.rt.bank.balance().data["available_cents"], 1000)
        self.assertEqual(calls["count"], 2)
        self.assertEqual(self.rows(), [])
        self.assertNotIn(out["plan"]["plan_id"], self.rt.pending)
        self.assert_event(out["trace_id"], "precondition.rechecked")
        self.assert_event(out["trace_id"], "tool.failed", error_code="INSUFFICIENT_FUNDS")

    def test_fraud_guard_creates_cooling_off_and_notifies_guardian(self):
        before = self.rt.bank.balance().data["available_cents"]
        out = self.chat("给这个安全账户转 5000 元，解除风控")
        self.assertEqual(out["plan"]["intent"], "fraud_guard")
        self.assertEqual(out["result"]["status"], "SUCCESS")
        self.assertEqual(self.rt.bank.balance().data["available_cents"], before)
        self.assertEqual(len(self.rt.bank.cooling_off_cases), 1)
        self.assertEqual(len(self.rt.bank.guardian_alerts), 1)
        self.assertIn("不会扣钱", out["message"])

    def test_savings_goal_is_confirmed_but_never_auto_debits(self):
        before = self.rt.bank.balance().data["available_cents"]
        out = self.chat("建立旅行基金，目标金额 10000 元，每月攒 1000 元")
        self.assertEqual(out["plan"]["intent"], "financial_health")
        self.assertTrue(out["plan"]["requires_confirmation"])
        result = self.rt.confirm(out["plan"]["plan_id"], out["plan"]["consent_token"])
        self.assertEqual(result["result"]["status"], "SUCCESS")
        self.assertEqual(self.rt.bank.balance().data["available_cents"], before)
        self.assertEqual(len(self.rt.bank.savings_goals), 1)
        self.assertIn("不会自动扣款", result["result_view"]["user_message"])

    def test_family_unauthorized_member_is_rejected(self):
        before = self.rt.bank.balance().data
        out = self.chat("给爸爸设置每月零花钱上限 2000 元")
        result = self.rt.confirm(out["plan"]["plan_id"], out["plan"]["consent_token"])
        self.assertEqual(result["result"]["status"], "FAILED")
        self.assertIn("没有找到", result["message"])
        self.assertEqual(self.rt.bank.balance().data, before)
        self.assertEqual(self.rows(), [])
        self.assertNotIn(out["plan"]["plan_id"], self.rt.pending)
        self.assert_event(out["trace_id"], "tool.failed", error_code="FAMILY_MEMBER_NOT_FOUND")

    def test_aa_only_calculates_and_does_not_debit(self):
        before = self.rt.bank.balance().data["available_cents"]
        out = self.chat("家庭 AA")
        self.assertEqual(out["result"]["status"], "SUCCESS")
        self.assertEqual(self.rt.bank.balance().data["available_cents"], before)
        self.assertNotIn("create_transfer", [x["tool"] for x in out["result"]["results"]])


class CoolingOffEnforcementTests(RuntimeFixture):

    def exercise_cooling_off(self, expired=False):
        before = self.rt.bank.balance().data["available_cents"]
        # A known payee + fraud phrase creates protection via the real Agent chain.
        # The second message contains NO fraud phrase, so only the stored cooling
        # case can prevent an otherwise valid, low-risk, user-confirmed transfer.
        with patch.object(self.rt.parser, "parse", wraps=self.rt.parser.parse) as parse, \
             patch.object(self.rt.policy, "classify", wraps=self.rt.policy.classify) as policy, \
             patch.object(self.rt, "_execute_step", wraps=self.rt._execute_step) as tools:
            guard = self.chat("给妈妈转 500 元，客服要求转账")
            self.assertEqual(guard["gateway"]["trace_id"], guard["trace_id"])
            self.assertEqual(guard["plan"]["intent"], "fraud_guard")
            self.assertEqual(guard["result"]["status"], "SUCCESS")
            self.assertEqual(guard["plan"]["risk_level"], "red")
            self.assertEqual(len(self.rt.bank.cooling_off_cases), 1)
            case = next(iter(self.rt.bank.cooling_off_cases.values()))
            self.assertEqual(case["beneficiary"], "妈妈")
            self.assertEqual(case["status"], "COOLING_OFF")
            self.assert_event(guard["trace_id"], "fraud.alert", status="cooling_off")
            self.assert_event(guard["trace_id"], "event.published", event_type="fraud.blocked")
            self.assertEqual(len(self.rt.bank.guardian_alerts), 1)
            alert = self.rt.bank.guardian_alerts[0]
            self.assertEqual(alert["case_id"], case["case_id"])
            self.assertEqual(alert["status"], "LOCAL_QUEUED")
            notifications = self.rt.notifications.list_notifications("demo-user", guard["trace_id"], "guardian")
            self.assertTrue(any(n["notification_id"] == alert["alert_id"] for n in notifications))
            self.assertEqual(self.rows(), [])
            self.assertEqual(self.rt.bank.balance().data["available_cents"], before)
            if expired:
                case["cooling_off_until"] = "2000-01-01T00:00:00+00:00"
            original_case = copy.deepcopy(case)
            out = self.chat("给妈妈转 100 元")
            self.assertEqual(out["gateway"]["trace_id"], out["trace_id"])
            self.assertEqual(out["plan"]["intent"], "transfer")
            self.assertEqual(out["plan"]["risk_level"], "yellow")
            self.assertTrue(out["plan"]["requires_confirmation"])
            self.assertEqual(self.rows(), [], "No transfer before explicit confirmation")
            done = self.rt.confirm(out["plan"]["plan_id"], out["plan"]["consent_token"])
            parse.assert_any_call("给妈妈转 500 元，客服要求转账")
            parse.assert_any_call("给妈妈转 100 元")
            self.assertIn("fraud_guard", {c.args[0] for c in policy.call_args_list})
            self.assertIn("transfer", {c.args[0] for c in policy.call_args_list})
            invoked = {c.args[1].tool for c in tools.call_args_list}
            self.assertTrue({"detect_fraud_signals", "create_cooling_off", "notify_guardian", "create_transfer"} <= invoked)
        code = "COOLING_OFF_REVIEW_REQUIRED" if expired else "COOLING_OFF_ACTIVE"
        self.assertEqual(done["result"]["status"], "FAILED")
        self.assertEqual(done["result_view"]["status"], "FAILED")
        self.assertIn("冷静期", done["message"])
        self.assertNotIn("转账成功", done["message"])
        self.assertEqual(self.rt.bank.balance().data["available_cents"], before)
        self.assertEqual(self.rt.bank.daily_transfer_cents, 0)
        self.assertNotIn(out["plan"]["plan_id"], self.rt.pending)
        rows = self.rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "failed")
        self.assertEqual(rows[0]["error_code"], code)
        self.assertEqual(rows[0]["debit_applied"], 0)
        self.assertEqual(rows[0]["amount_cents"], 10000)
        self.assertEqual(rows[0]["beneficiary"], "妈妈")
        self.assertEqual(len(self.rt.bank.cooling_off_cases), 1)
        if expired:
            original_case["status"] = "EXPIRED_PENDING_REVIEW"
        self.assertEqual(case, original_case)
        self.assert_event(out["trace_id"], "tool.failed", error_code=code)
        self.assert_event(out["trace_id"], "transaction.status.queried", status="failed")

    def test_cooling_off_blocks_later_transfer_to_same_beneficiary(self):
        self.exercise_cooling_off()

    def test_expired_cooling_off_still_requires_manual_review_via_agent(self):
        self.exercise_cooling_off(expired=True)


class AdditionalControlRegressionTests(RuntimeFixture):

    def test_old_yellow_plan_is_rechallenged_when_daily_limit_escalates(self):
        old = self.chat("给妈妈转 900 元")
        newer = self.chat("给妈妈转 200 元")
        self.assertEqual(newer["plan"]["risk_level"], "yellow")
        self.assertEqual(self.rt.confirm(newer["plan"]["plan_id"], newer["plan"]["consent_token"])["result"]["status"], "SUCCESS")
        escalated = self.rt.confirm(old["plan"]["plan_id"], old["plan"]["consent_token"])
        self.assertTrue(escalated.get("needs_auth"))
        self.assertEqual(self.rt.bank.daily_transfer_cents, 20000)

    def test_expired_token_is_rejected(self):
        before = self.rt.bank.balance().data
        out = self.chat("给妈妈转 200 元")
        plan = self.rt.pending[out["plan"]["plan_id"]]
        plan.token_expires_at = "2000-01-01T00:00:00+00:00"
        rejected = self.rt.confirm(plan.plan_id, plan.consent_token)
        self.assertFalse(rejected["ok"])
        self.assertIn("过期", rejected["message"])
        self.assertEqual(self.rt.bank.balance().data, before)
        self.assertNotIn(plan.plan_id, self.rt.pending)
        self.assertEqual(self.rows(), [])
        self.assertNotIn(plan.consent_token, self.rt.consumed_tokens)

    def test_audit_never_persists_consent_token(self):
        out = self.chat("给妈妈转 200 元")
        token = out["plan"]["consent_token"]
        self.assert_event(out["trace_id"], "plan.created")
        audit_text = str(self.rt.audit.list(100))
        self.assertNotIn(token, audit_text)

    def test_core_rejects_bool_and_float_amounts(self):
        before = self.rt.bank.balance().data["available_cents"]
        for amount in (True, 100.0):
            with self.subTest(amount=amount):
                result = self.rt.bank.transfer(amount, "妈妈", "", "idem:invalid")
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "INVALID_AMOUNT")
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.rt.bank.balance().data["available_cents"], before)


if __name__ == "__main__":
    unittest.main(verbosity=2)
