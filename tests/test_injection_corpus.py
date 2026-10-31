import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))
import banking_agent as ba


class InjectionCorpusTests(unittest.TestCase):
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

    def _load_corpus(self):
        path = ROOT / "eval" / "injection_corpus.json"
        with path.open(encoding="utf-8") as handle:
            corpus = json.load(handle)
        self.assertIsInstance(corpus, list)
        self.assertGreaterEqual(len(corpus), 30)
        return corpus

    def test_every_red_team_sample_is_blocked_and_audited(self):
        corpus = self._load_corpus()
        categories = set()
        for sample in corpus:
            with self.subTest(sample=sample.get("id")):
                self.assertTrue(sample.get("id"))
                self.assertIsInstance(sample.get("text"), str)
                self.assertTrue(sample["text"].strip())
                self.assertTrue(sample.get("category"))
                self.assertEqual(sample.get("expected_behavior"), "block_and_audit")
                categories.add(sample["category"])

                allowed, reason = ba.InjectionGuard.inspect(sample["text"])
                self.assertFalse(allowed)
                self.assertTrue(reason)
                result = self.runtime.chat(sample["text"])
                self.assertFalse(result["ok"])
                self.assertTrue(result["blocked"])
                self.assertIn("trace_id", result)

        self.assertTrue({
            "instruction_override",
            "role_play",
            "delimiter_spoofing",
            "context_poisoning",
            "amount_tampering",
        }.issubset(categories))
        events = self.runtime.audit.list(limit=len(corpus) + 1)
        self.assertEqual(len(events), len(corpus))
        self.assertTrue(all(event["event_type"] == "injection_blocked" for event in events))
        self.assertTrue(all(event["payload"].get("reason") for event in events))


if __name__ == "__main__":
    unittest.main(verbosity=2)
