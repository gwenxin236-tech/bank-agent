import copy
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import Mock, patch
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

import production_contracts as pc
import production_backends as pb
import banking_agent as ba


class ProductionContractTests(unittest.TestCase):
    def setUp(self):
        self.ctx = pc.AdapterContext(trace_id="trace-test", actor_id="user-test")

    def test_manifest_lists_real_production_replacement_boundaries(self):
        manifest = pc.production_contract_manifest()
        self.assertEqual(manifest["status"], "DEMO_CONTRACTS_ONLY")
        for key in ("identity", "external_systems", "state_and_messaging", "security_operations", "availability", "quality_gates"):
            self.assertTrue(manifest[key], key)
        self.assertIn("compensation_required_for_side_effects", manifest["retry_policy"])
        self.assertIn("create_transfer", manifest["retry_policy"]["never_retry"])
        self.assertIn("RedisTaskStore", manifest["optional_adapters"]["backends"])
        self.assertEqual(manifest["preflight"]["endpoint"], "/api/preflight")

    def test_mock_strong_auth_binds_challenge_to_operation_digest(self):
        # Each attack gets an UNUSED challenge: replay must not mask binding failure.
        attacks = [
            (self.ctx, "changed-digest", "demo-assertion"),
            (replace(self.ctx, actor_id="other-user"), "digest-123", "demo-assertion"),
            (replace(self.ctx, device_id="other-device"), "digest-123", "demo-assertion"),
            (self.ctx, "digest-123", ""),
        ]
        for ctx, digest, assertion in attacks:
            with self.subTest(actor=ctx.actor_id, device=ctx.device_id, digest=digest, assertion=assertion):
                provider = pc.MockStrongAuthProvider()
                begun = provider.begin(self.ctx, "digest-123", "demo")
                self.assertTrue(begun.ok)
                challenge_id = begun.data["challenge_id"]
                before = copy.deepcopy(provider._challenges)
                rejected = provider.verify(ctx, challenge_id, assertion, digest)
                self.assertFalse(rejected.ok)
                self.assertEqual(rejected.code, "AUTH_BINDING_MISMATCH")
                self.assertEqual(provider._challenges, before, "Rejected binding must not consume/mutate challenge")
                verified = provider.verify(self.ctx, challenge_id, "demo-assertion", "digest-123")
                self.assertTrue(verified.ok)
                self.assertEqual(verified.code, "MOCK_AUTH_OK")
                self.assertEqual(verified.data["bound_digest"], "digest-123")
                self.assertTrue(provider._challenges[challenge_id]["used"])
                self.assertEqual(verified.trace_id, self.ctx.trace_id)
                consumed = copy.deepcopy(provider._challenges)
                replay = provider.verify(self.ctx, challenge_id, "demo-assertion", "digest-123")
                self.assertFalse(replay.ok)
                self.assertEqual(replay.code, "AUTH_REPLAY")
                self.assertEqual(provider._challenges, consumed)

    def test_mock_bank_core_rejects_negative_float_bool_and_non_integer_amounts(self):
        core = pc.MockBankCoreAdapter()
        balance = core.get_balance(self.ctx).data
        for amount in (-1, 0, 1.2, True, "100"):
            with self.subTest(amount=amount):
                result = core.create_transfer(self.ctx, amount, "mom", "test")
                self.assertFalse(result.ok)
                self.assertEqual(result.code, "INVALID_AMOUNT")
                self.assertEqual(result.trace_id, self.ctx.trace_id)
                self.assertEqual(core._transactions, {})
                self.assertEqual(core._idempotency, {})
                self.assertEqual(core.get_balance(self.ctx).data, balance)
        ok = core.create_transfer(self.ctx, 100, "mom", "test")
        self.assertTrue(ok.ok)
        self.assertEqual(ok.data["amount_cents"], 100)
        self.assertEqual(ok.data["status"], "succeeded")
        self.assertEqual(len(core._transactions), 1)
        self.assertEqual(len(core._idempotency), 1)
        self.assertEqual(core.query_transaction(self.ctx, ok.data["transaction_id"]).data, ok.data)

    def test_local_field_encryptor_fixture_round_trip_and_wrong_field_decode_error(self):
        encryptor = pc.LocalFieldEncryptor("unit-test-secret")
        ciphertext = encryptor.encrypt("13800138000", "phone")
        self.assertNotEqual(ciphertext, "13800138000")
        self.assertEqual(encryptor.decrypt(ciphertext, "phone"), "13800138000")
        # XOR demo is NOT authenticated encryption; this fixture must fail decoding.
        with self.assertRaises(UnicodeDecodeError):
            encryptor.decrypt(ciphertext, "account_no")

    def test_in_memory_queue_supports_message_and_dead_letter(self):
        queue = pc.InMemoryQueue()
        payload = {"trace_id": "t", "task_id": "task-1"}
        published = queue.publish("agent.tasks", payload, "message-1")
        self.assertTrue(published.ok)
        self.assertEqual(published.code, "MESSAGE_ENQUEUED")
        self.assertEqual(published.trace_id, "t")
        self.assertEqual(published.idempotency_key, "message-1")
        self.assertEqual(len(queue.messages), 1)
        self.assertEqual(queue.dead_letters, [])
        self.assertEqual(queue.messages[0]["topic"], "agent.tasks")
        self.assertEqual(queue.messages[0]["message_id"], "message-1")
        self.assertEqual(queue.messages[0]["payload"], payload)
        before = copy.deepcopy(queue.messages)
        dead = queue.dead_letter("agent.tasks", payload, "max attempts")
        self.assertTrue(dead.ok)
        self.assertEqual(dead.code, "MESSAGE_DEAD_LETTERED")
        self.assertEqual(dead.trace_id, "t")
        self.assertEqual(queue.messages, before)
        self.assertEqual(len(queue.dead_letters), 1)
        self.assertEqual(queue.dead_letters[0]["topic"], "agent.tasks")
        self.assertEqual(queue.dead_letters[0]["reason"], "max attempts")
        self.assertEqual(queue.dead_letters[0]["payload"], payload)

    def test_optional_backend_factory_never_silently_falls_back(self):
        with self.assertRaises(ValueError):
            pb.build_configured_backends("redis", "sqlite-hash-chain")
        with self.assertRaises(ValueError):
            pb.build_configured_backends("memory", "postgres-worm")

    def test_optional_backend_dependency_errors_are_explicit(self):
        # Environment-independent: always exercise the absent dependency branch.
        with patch.object(pb.importlib, "import_module", side_effect=ImportError("fixture absent")) as load:
            with self.assertRaisesRegex(pb.BackendDependencyError, "requirements-production.txt") as raised:
                pb.RedisTaskStore("redis://127.0.0.1:6379/0")
        load.assert_called_once_with("redis")
        self.assertIsInstance(raised.exception.__cause__, ImportError)

    def test_optional_backend_constructs_supplied_client_without_network(self):
        client = object()
        factory = Mock(return_value=client)
        module = SimpleNamespace(Redis=SimpleNamespace(from_url=factory))
        with patch.object(pb.importlib, "import_module", return_value=module) as load:
            store = pb.RedisTaskStore("redis://fixture:6379/0", prefix="test:", ttl_seconds=17)
        load.assert_called_once_with("redis")
        factory.assert_called_once_with("redis://fixture:6379/0", decode_responses=True,
                                       socket_timeout=2, socket_connect_timeout=2)
        self.assertIs(store.client, client)
        self.assertEqual(store._key("task-1"), "test:task-1")
        self.assertEqual(store.ttl_seconds, 17)

    def test_optional_backend_does_not_swallow_unrelated_import_errors(self):
        with patch.object(pb.importlib, "import_module", side_effect=ValueError("fixture invalid")):
            with self.assertRaisesRegex(ValueError, "fixture invalid"):
                pb.RedisTaskStore("redis://fixture:6379/0")

    def test_task_store_round_trip(self):
        store = pc.InMemoryTaskStore()
        original = {"status": "PLANNED", "trace_id": "t"}
        saved = store.put("task-1", original)
        original["status"] = "CALLER_MUTATION"
        loaded = store.get("task-1")
        self.assertTrue(saved.ok)
        self.assertEqual(saved.code, "TASK_SAVED")
        self.assertEqual(saved.data, {"task_id": "task-1"})
        self.assertTrue(loaded.ok)
        self.assertEqual(loaded.code, "TASK_FOUND")
        self.assertEqual(loaded.data, {"status": "PLANNED", "trace_id": "t"})
        self.assertEqual(loaded.trace_id, "t")
        self.assertTrue(store.update("task-1", {"status": "CANCELLED", "trace_id": "t"}).ok)
        self.assertEqual(store.get("task-1").data, {"status": "CANCELLED", "trace_id": "t"})
        missing = store.get("missing")
        self.assertFalse(missing.ok)
        self.assertEqual(missing.code, "TASK_NOT_FOUND")
        self.assertEqual(missing.data, {})
        self.assertEqual(set(store._items), {"task-1"})


class ProductionEndpointTests(unittest.TestCase):
    def test_production_contract_endpoint_returns_manifest(self):
        # Exercise routing rather than looking for a source-code string.
        handler = object.__new__(ba.Handler)
        handler.path = "/api/production/contracts"
        handler.headers = {}
        handler._client_key = lambda: "d3-contract"
        sent = []
        handler._send = lambda status, data, *args: sent.append((status, data))
        with patch.object(ba.http_rate_limiter, "allow", return_value=True):
            handler.do_GET()
        self.assertEqual(len(sent), 1, "Endpoint must send exactly one response")
        status, response = sent[0]
        self.assertEqual(status, 200)
        self.assertTrue(response["ok"])
        self.assertEqual(response["contracts"], pc.production_contract_manifest())
        self.assertEqual(response["contracts"]["status"], "DEMO_CONTRACTS_ONLY")


if __name__ == "__main__":
    unittest.main(verbosity=2)
