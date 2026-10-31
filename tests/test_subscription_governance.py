"""E4: subscription governance keeps debit authorization, service and refund facts separate."""
import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import banking_agent as ba
from tool_contract import ToolContext


class SubscriptionGovernanceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.runtime = ba.AgentRuntime(db_path=Path(self.tmp.name) / "e4.sqlite3", offline=True)

    def tearDown(self):
        self.runtime.recovery_scheduler.stop()
        self.runtime.bank.close()
        self.runtime.audit.db.close()
        self.tmp.cleanup()

    def _confirm(self, text):
        planned = self.runtime.chat(text)
        self.assertEqual(planned["plan"]["intent"], "subscription_cancel")
        self.assertTrue(planned["plan"]["entities"]["subscription_id"])
        return planned, self.runtime.confirm(planned["plan"]["plan_id"], planned["plan"]["consent_token"])

    def test_query_exposes_three_layers_and_governance_markers(self):
        result = self.runtime.bank.subscriptions_query()
        self.assertTrue(result.ok)
        items = result.data["items"]
        self.assertGreaterEqual(len(items), 2)
        for item in items:
            self.assertIn("transaction_layer", item)
            self.assertIn("history", item["transaction_layer"])
            self.assertIn("authorization_layer", item)
            self.assertIn("authorized_subject", item["authorization_layer"])
            self.assertIn("debit_permission", item["authorization_layer"])
            self.assertIn("service_layer", item)
            self.assertIn("member_status", item["service_layer"])
            self.assertIn("valid_until", item["service_layer"])
            self.assertIn("refund_status", item["service_layer"])
            self.assertTrue(item["governance_markers"])
        markers = {marker for item in items for marker in item["governance_markers"]}
        self.assertTrue({"已确认订阅", "疑似周期扣费", "当前渠道可处理", "需服务商处理", "待核验"} <= markers)

    def test_query_waits_for_yellow_confirmation_before_reading_core(self):
        with patch.object(self.runtime.bank, 'subscriptions_query', wraps=self.runtime.bank.subscriptions_query) as query:
            planned = self.runtime.chat('查询自动续费订阅')
            self.assertEqual(planned['plan']['intent'], 'subscription_query')
            self.assertEqual(planned['plan_view']['risk_level'], 'yellow')
            self.assertTrue(planned['plan_view']['requires_confirmation'])
            self.assertFalse(planned['plan_view']['requires_strong_auth'])
            self.assertTrue(planned['plan_view']['action_token'])
            self.assertIn('自动续费', planned['plan_view']['summary'])
            self.assertNotIn('result', planned)
            query.assert_not_called()

    def test_confirmed_query_is_read_only_audited_and_cannot_be_replayed(self):
        before = copy.deepcopy(self.runtime.bank.subscriptions)
        balance = self.runtime.bank.balance().data
        with patch.object(self.runtime.bank, 'subscriptions_query', wraps=self.runtime.bank.subscriptions_query) as query:
            planned = self.runtime.chat('查询自动续费订阅')
            plan_id, token = planned['plan']['plan_id'], planned['plan_view']['action_token']
            completed = self.runtime.confirm(plan_id, token)
            self.assertTrue(completed['ok'])
            self.assertEqual(completed['result']['status'], 'SUCCESS')
            data = next(x['data'] for x in completed['result']['results'] if x['tool'] == 'list_subscriptions')
            self.assertEqual(data['items'], before)
            self.assertFalse(completed['plan_view']['requires_confirmation'])
            self.assertEqual(completed['plan_view']['action_token'], '')
            self.assertFalse(self.runtime.confirm(plan_id, token)['ok'])
            query.assert_called_once()
        self.assertEqual(self.runtime.bank.subscriptions, before)
        self.assertEqual(self.runtime.bank.balance().data, balance)
        events = [e['event_type'] for e in self.runtime.audit.list_for_trace(planned['trace_id'])]
        self.assertIn('consent.granted', events)
        self.assertIn('tool.result', events)
        self.assertLess(events.index('consent.granted'), events.index('tool.result'))
        self.assertTrue(self.runtime.audit.verify_trace(planned['trace_id'])['valid'])

    def test_query_rejects_missing_wrong_and_cross_plan_tokens(self):
        planned = self.runtime.chat('查询自动续费订阅')
        other = self.runtime.chat('查询自动续费订阅')
        with patch.object(self.runtime.bank, 'subscriptions_query', wraps=self.runtime.bank.subscriptions_query) as query:
            for token in ('', 'invalid-token', other['plan_view']['action_token']):
                with self.subTest(token_type='empty' if not token else 'invalid-or-other-plan'):
                    self.assertFalse(self.runtime.confirm(planned['plan']['plan_id'], token)['ok'])
            query.assert_not_called()

    def test_expired_query_confirmation_never_reads_core(self):
        planned = self.runtime.chat('查询自动续费订阅')
        self.assertIn(planned['plan']['plan_id'], self.runtime.pending)
        self.runtime.pending[planned['plan']['plan_id']].token_expires_at = (
            datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
        with patch.object(self.runtime.bank, 'subscriptions_query', wraps=self.runtime.bank.subscriptions_query) as query:
            self.assertFalse(self.runtime.confirm(planned['plan']['plan_id'], planned['plan_view']['action_token'])['ok'])
            query.assert_not_called()

    def test_cancelled_query_confirmation_never_reads_core(self):
        planned = self.runtime.chat('查询自动续费订阅')
        with patch.object(self.runtime.bank, 'subscriptions_query', wraps=self.runtime.bank.subscriptions_query) as query:
            self.assertTrue(self.runtime.cancel(planned['plan']['plan_id'], planned['plan_view']['action_token'])['ok'])
            self.assertFalse(self.runtime.confirm(planned['plan']['plan_id'], planned['plan_view']['action_token'])['ok'])
            query.assert_not_called()

    def test_query_tool_failure_does_not_claim_success_or_change_subscriptions(self):
        planned = self.runtime.chat('查询自动续费订阅')
        before = copy.deepcopy(self.runtime.bank.subscriptions)
        failure = ba.ToolResult(False, 'list_subscriptions', error_code='UNAVAILABLE', error_message='订阅查询暂不可用')
        with patch.object(self.runtime.bank, 'subscriptions_query', return_value=failure):
            completed = self.runtime.confirm(planned['plan']['plan_id'], planned['plan_view']['action_token'])
        self.assertNotEqual(completed['result']['status'], 'SUCCESS')
        self.assertFalse(completed['result_view']['ok'])
        self.assertEqual(self.runtime.bank.subscriptions, before)

    def test_plan_and_receipt_separate_debit_service_and_refund(self):
        planned, completed = self._confirm("取消某视频会员，退款到账了吗")
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["result"]["status"], "SUCCESS")
        data = next(x["data"] for x in completed["result"]["results"] if x["tool"] == "cancel_subscription")
        layers = data["layers"]
        self.assertEqual(layers["debit_authorization"]["status"], "stopped")
        self.assertEqual(layers["service"]["status"], "active")
        self.assertEqual(layers["refund"]["status"], "unknown")
        self.assertEqual(layers["refund"]["amount_cents"], 0)
        self.assertIn("停止未来扣费", completed["result_view"]["user_message"])
        self.assertIn("服务记录中的到期日", completed["result_view"]["user_message"])
        self.assertIn("当前使用权益请向服务商核实", completed["result_view"]["user_message"])
        self.assertIn("退款状态待核验", completed["result_view"]["user_message"])
        self.assertIn("停止未来扣费", " ".join(planned["plan_view"]["details"]))
        self.assertNotIn("退款已到账", completed["result_view"]["user_message"])
        # Cancelling future debit is not a cash credit and cannot alter the balance.
        self.assertEqual(self.runtime.bank.balance().data["available_cents"], ba.MockBankCore.INITIAL_BALANCE_CENTS)

    def test_cancel_idempotency_returns_same_layered_result(self):
        before = copy.deepcopy(self.runtime.bank.subscriptions)
        first = self.runtime.bank.cancel_subscription("sub_001", "idem:e4:sub_001")
        second = self.runtime.bank.cancel_subscription("sub_001", "idem:e4:sub_001")
        self.assertTrue(first.ok and second.ok)
        self.assertEqual(second.data["subscription_id"], first.data["subscription_id"])
        self.assertEqual(second.data["layers"], first.data["layers"])
        self.assertTrue(second.data["idempotent_replay"])
        self.assertEqual(self.runtime.bank.subscriptions[0]["authorization_layer"]["debit_permission"], "stopped")
        self.assertNotEqual(self.runtime.bank.subscriptions, before)

    def test_unknown_cancel_is_pending_and_does_not_claim_success_or_mutate(self):
        before = copy.deepcopy(self.runtime.bank.subscriptions)
        self.runtime.bank.inject_fault("transport_timeout")
        planned, completed = self._confirm("取消某视频会员，退款到账了吗")
        self.assertTrue(completed["ok"])
        self.assertEqual(completed["result"]["status"], "UNKNOWN")
        self.assertEqual(completed["result_view"]["status"], "UNKNOWN")
        self.assertIn("待处理", completed["result_view"]["user_message"])
        self.assertNotIn("已取消", completed["result_view"]["user_message"])
        self.assertEqual(self.runtime.bank.subscriptions, before)
        self.assertTrue(any(e["event_type"] == "subscription.unknown" for e in self.runtime.audit.list_for_trace(planned["trace_id"])))

    def test_feature_registry_has_read_only_governance_tool(self):
        tool = ba.FEATURE_REGISTRY.get("finance_health_subscription_governance")
        self.assertIsNotNone(tool)
        self.assertFalse(tool.side_effect)
        result = tool.execute(ToolContext("trace", "task", "node", "demo-user", "demo-user", "demo-device", True), query_text="退款到账了吗")
        self.assertTrue(result["ok"])
        self.assertIn("transaction_layer", result["data"])
        self.assertIn("authorization_layer", result["data"])
        self.assertIn("service_layer", result["data"])


if __name__ == "__main__":
    unittest.main()
