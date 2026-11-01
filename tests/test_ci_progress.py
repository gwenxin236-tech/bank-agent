"""CI diagnostics must fail stalled workers and preserve assertion failures."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


class CIProgressTests(unittest.TestCase):
    def test_module_shards_are_disjoint_complete_and_keep_module_fixtures_together(self):
        from ci_verify import partition
        cases = []
        for module, count in [('alpha', 7), ('beta', 3), ('gamma', 5), ('delta', 2), ('epsilon', 4)]:
            cls = type('Fixture', (unittest.TestCase,), {'__module__': module, 'runTest': lambda self: None})
            cases.extend(cls() for _ in range(count))
        groups = [partition(cases, i, 4) for i in range(4)]
        self.assertEqual(sorted(id(t) for g in groups for t in g), sorted(map(id, cases)))
        for module in {t.__class__.__module__ for t in cases}:
            self.assertEqual(sum(any(t.__class__.__module__ == module for t in g) for g in groups), 1)
        self.assertEqual([[id(t) for t in g] for g in groups],
                         [[id(t) for t in partition(cases, i, 4)] for i in range(4)])

    def test_invalid_shards_are_rejected_and_single_shard_preserves_order(self):
        from ci_verify import partition
        cases = [unittest.FunctionTestCase(lambda: None)]
        self.assertEqual(partition(cases, 0, 1), cases)
        for index, count in [(-1, 4), (4, 4), (0, 0), (0, 17)]:
            with self.subTest(index=index, count=count), self.assertRaises(ValueError):
                partition(cases, index, count)

    def run_child(self, body):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'probe.json'
            code = """import sys,unittest,threading,json
from pathlib import Path
sys.path.insert(0,str(Path.cwd()/'scripts'))
from ci_verify import run_suite
path=Path(sys.argv[1])
""" + body
            process = subprocess.run([sys.executable, '-B', '-c', code, str(report)],
                                     cwd=Path(__file__).resolve().parents[1],
                                     capture_output=True, timeout=15,
                                     env=dict(os.environ, PYTHONDONTWRITEBYTECODE='1'))
            return process, json.loads(report.read_text()) if report.exists() else None

    def test_stalled_worker_fails_with_stack_locations_without_local_values(self):
        process, report = self.run_child("""
def blocked(self):
 private_value='NEVER_LOG_THIS_LOCAL_VALUE'
 threading.Event().wait(10)
case=type('BlockedCase',(unittest.TestCase,),{'test_blocked':blocked})
run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(case),path,0.2)
""")
        self.assertEqual(process.returncode, 124)
        self.assertEqual(report['status'], 'FAIL')
        self.assertIn('test_blocked', report['active_test'])
        self.assertTrue(report['threads'])
        self.assertNotIn('NEVER_LOG_THIS_LOCAL_VALUE', json.dumps(report))

    def test_diagnostics_do_not_convert_assertion_failure_to_success(self):
        process, report = self.run_child("""
def passed(self): self.assertTrue(True)
def failed(self): self.assertEqual(1,2)
case=type('Checks',(unittest.TestCase,),{'test_passed':passed,'test_failed':failed})
result=run_suite(unittest.defaultTestLoader.loadTestsFromTestCase(case),path,5)
assert result.testsRun==2 and len(result.failures)==1 and not result.skipped
raise SystemExit(0 if result.wasSuccessful() else 1)
""")
        self.assertEqual(process.returncode, 1)
        self.assertIn(b'Ran 2 tests', process.stderr)
        self.assertIsNone(report)
