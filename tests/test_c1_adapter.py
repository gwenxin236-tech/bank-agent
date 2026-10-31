import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import production_contracts as pc


class C1AdapterContractTests(unittest.TestCase):
    def test_mock_core_adapter_is_idempotent_and_exposes_final_status_and_reversal(self):
        core = pc.MockBankCoreAdapter()
        ctx = pc.AdapterContext(trace_id="trace-core", actor_id="user-test", idempotency_key="idem-core-1")
        first = core.create_transfer(ctx, 100, "mom", "test")
        replay = core.create_transfer(ctx, 100, "mom", "test")
        self.assertEqual(first.data["transaction_id"], replay.data["transaction_id"])
        self.assertEqual(replay.data["status"], "succeeded")
        self.assertTrue(replay.data["idempotent_replay"])
        queried = core.query_transaction(ctx, first.data["transaction_id"])
        self.assertEqual(queried.data["status"], "succeeded")
        reversed_result = core.reverse_transfer(ctx, first.data["transaction_id"], "演示冲正")
        self.assertEqual(reversed_result.data["status"], "reversed")
        self.assertEqual(core.query_transaction(ctx, first.data["transaction_id"]).data["status"], "reversed")


if __name__ == "__main__":
    unittest.main(verbosity=2)
