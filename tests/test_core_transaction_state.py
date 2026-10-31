import os
import tempfile
import unittest
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba


class CoreTransactionStateTests(unittest.TestCase):
    def setUp(self):
        self.fd, self.db_name = tempfile.mkstemp(suffix=".sqlite3")
        os.close(self.fd)
        self.db_path = Path(self.db_name)
        # Cleanups run in reverse order, so unlink only after all handles close.
        self.addCleanup(self.db_path.unlink)

    def _core(self, **kwargs):
        core = ba.MockBankCore(db_path=self.db_path, **kwargs)
        self.addCleanup(core.close)
        return core

    def _runtime(self):
        runtime = ba.AgentRuntime()
        self.addCleanup(runtime.audit.db.close)
        self.addCleanup(runtime.bank.close)
        self.addCleanup(runtime.recovery_scheduler.stop)
        return runtime

    def test_display_audit_ids_are_not_mistaken_for_sensitive_digits(self):
        import uuid
        from unittest.mock import patch
        with patch.object(ba.uuid, 'uuid4', return_value=uuid.UUID('12345678123456781234567812345678')):
            identifier = ba.new_id('tx')
        self.assertEqual(ba.sanitize_audit_value(identifier), identifier)

    def test_transaction_state_machine_reaches_succeeded_and_is_queryable(self):
        core = self._core()
        result = core.transfer(20_000, "妈妈", "午餐", "idem-state-1")

        self.assertTrue(result.ok)
        self.assertEqual(result.data["status"], "succeeded")
        self.assertEqual(result.data["state_history"], ["created", "processing", "succeeded"])
        queried = core.query_transaction(result.data["transaction_id"])
        self.assertTrue(queried.ok)
        self.assertEqual(queried.data["status"], "succeeded")
        self.assertEqual(queried.data["amount_cents"], 20_000)

    def test_same_idempotency_key_replays_processing_without_second_debit(self):
        core = self._core(fault_injection="timeout")
        first = core.transfer(20_000, "妈妈", "超时演练", "idem-timeout-1")
        balance = core.balance().data["available_cents"]
        second = core.transfer(20_000, "妈妈", "超时演练", "idem-timeout-1")

        self.assertTrue(first.ok)
        self.assertEqual(first.data["status"], "processing")
        self.assertEqual(second.data["transaction_id"], first.data["transaction_id"])
        self.assertEqual(second.data["status"], "processing")
        self.assertTrue(second.data["idempotent_replay"])
        self.assertEqual(core.balance().data["available_cents"], balance)
        self.assertEqual(len(core.transactions), 5)

    def test_injected_insufficient_funds_and_missing_beneficiary_fail_without_debit(self):
        insufficient = self._core(fault_injection="insufficient_funds")
        before = insufficient.balance().data["available_cents"]
        failed = insufficient.transfer(20_000, "妈妈", "余额不足演练", "idem-insufficient")
        self.assertFalse(failed.ok)
        self.assertEqual(failed.error_code, "INSUFFICIENT_FUNDS")
        self.assertEqual(failed.data["status"], "failed")
        self.assertEqual(insufficient.balance().data["available_cents"], before)

        missing = self._core(fault_injection="beneficiary_not_found")
        failed_recipient = missing.transfer(20_000, "不存在的人", "收款人演练", "idem-recipient")
        self.assertFalse(failed_recipient.ok)
        self.assertEqual(failed_recipient.error_code, "BENEFICIARY_NOT_FOUND")
        self.assertEqual(failed_recipient.data["status"], "failed")

    def test_processing_survives_restart_and_can_be_finalized_once(self):
        core = self._core(fault_injection="timeout")
        pending = core.transfer(20_000, "妈妈", "重启演练", "idem-restart-1")
        tx_id = pending.data["transaction_id"]
        before = core.balance().data["available_cents"]
        core.close()

        restarted = self._core()
        recovered = restarted.query_transaction(tx_id)
        self.assertEqual(recovered.data["status"], "processing")
        self.assertEqual(restarted.balance().data["available_cents"], before)

        finalized = restarted.finalize_transaction(tx_id, outcome="succeeded")
        self.assertEqual(finalized.data["status"], "succeeded")
        after = restarted.balance().data["available_cents"]
        replay = restarted.transfer(20_000, "妈妈", "重启演练", "idem-restart-1")
        self.assertEqual(replay.data["transaction_id"], tx_id)
        self.assertEqual(replay.data["status"], "succeeded")
        self.assertTrue(replay.data["idempotent_replay"])
        self.assertEqual(restarted.balance().data["available_cents"], after)

    def test_agent_reports_processing_without_claiming_success(self):
        old_db = ba.DB_PATH
        ba.DB_PATH = self.db_path
        try:
            runtime = self._runtime()
            runtime.bank.fault_injection = "timeout"
            planned = runtime.chat("给妈妈转 200 元")
            result = runtime.confirm(planned["plan"]["plan_id"], planned["plan"]["consent_token"])
            self.assertEqual(result["result"]["status"], "PROCESSING")
            self.assertEqual(result["result_view"]["status"], "PROCESSING")
            self.assertIn("银行还在确认这笔交易，请不要重复操作", result["result_view"]["user_message"])
            self.assertNotIn("已向妈妈转账", result["result_view"]["user_message"])
        finally:
            ba.DB_PATH = old_db

    def test_audit_transaction_status_matches_final_accounting_state(self):
        old_db = ba.DB_PATH
        ba.DB_PATH = self.db_path
        try:
            runtime = self._runtime()
            planned = runtime.chat("给妈妈转 200 元")
            completed = runtime.confirm(planned["plan"]["plan_id"], planned["plan"]["consent_token"])
            tx_id = completed["result"]["results"][-1]["data"]["transaction_id"]
            tx = runtime.bank.query_transaction(tx_id).data
            events = runtime.audit.list_for_trace(planned["plan"]["trace_id"])
            tool_events = [event for event in events if event["event_type"] == "tool.result"]
            self.assertTrue(tool_events)
            self.assertEqual(tool_events[-1]["payload"]["tool_result"]["transaction_id"], tx_id)
            self.assertEqual(tool_events[-1]["payload"]["tool_result"]["status"], tx["status"])
        finally:
            ba.DB_PATH = old_db


if __name__ == "__main__":
    unittest.main(verbosity=2)
