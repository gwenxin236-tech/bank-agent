"""T07 security contracts; real containers are exercised by sandbox_qa.py."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import banking_agent as ba


class SandboxExecutionTests(unittest.TestCase):
    def setUp(self):
        from sandbox_execution import SandboxRunner
        self.tmp = tempfile.TemporaryDirectory()
        self.audit = ba.AuditStore(Path(self.tmp.name) / 'sandbox.sqlite3')
        self.breaker = ba.CircuitBreaker()
        self.runner = SandboxRunner(self.audit, self.breaker)

    def tearDown(self):
        if hasattr(self, 'audit'):
            self.audit.db.close()
            self.tmp.cleanup()

    def test_only_integer_calculation_subset_is_accepted(self):
        from sandbox_execution import validate_code
        for source in ('result = sum([100, 200, 300])', 'result = (1234 - 34) // 3',
                       'result = abs(-25) + len([1, 2])'):
            self.assertTrue(validate_code(source))

    def test_import_file_network_shell_and_reflection_are_rejected_before_docker(self):
        for source in ('import os', 'result = open("secret")',
                       'result = __import__("socket")', 'result = (1).__class__',
                       'result = eval("1+1")', 'while True: pass',
                       'result = [x for x in range(100)]', 'result = 2 ** 1000000',
                       'result = 1; result = 2'):
            with self.subTest(source=source):
                runner = type(self.runner)(self.audit, ba.CircuitBreaker())
                with patch.object(runner, '_launch') as launch:
                    result = runner.execute(source)
                self.assertEqual(result['status'], 'REJECTED')
                launch.assert_not_called()
                self.assertTrue(self.audit.verify_trace(result['trace_id'])['valid'])

    def test_float_bool_string_huge_source_and_deep_ast_are_rejected(self):
        from sandbox_execution import validate_code
        for source in ('result = 1.1', 'result = True', 'result = "hello"',
                       'result = 1000000001', 'result = sum([' + '1,' * 300 + '1])',
                       'result = ' + '+'.join(['1'] * 200), 'result = 1' + ' ' * 5000):
            with self.subTest(source_length=len(source)), self.assertRaises(ValueError):
                validate_code(source)

    def test_no_host_execution_when_docker_is_unavailable(self):
        with patch('sandbox_execution.shutil.which', return_value=None):
            runner = type(self.runner)(self.audit, self.breaker)
        result = runner.execute('result = 100 + 200')
        self.assertEqual(result['status'], 'UNAVAILABLE')
        self.assertFalse(result['ok'])

    def test_success_records_reconstructible_digests_without_raw_code(self):
        import hashlib
        source = 'result = sum([100, 200])'
        with patch.object(self.runner, '_launch', return_value={
            'status': 'SUCCESS', 'data': {'result': 300}, 'image_id': 'sha256:' + '1' * 64,
            'cleanup_ok': True, 'output_bytes': 14}):
            result = self.runner.execute(source)
        self.assertEqual(result['data']['result'], 300)
        self.assertEqual(result['scope'], 'LOCAL_ISOLATED_DEMO')
        events = self.audit.list_for_trace(result['trace_id'])
        self.assertIn('sandbox.started', [e['event_type'] for e in events])
        finished = next(e['payload'] for e in events if e['event_type'] == 'sandbox.finished')
        self.assertEqual(bytes(finished['source_digest']).hex(), hashlib.sha256(source.encode()).hexdigest())
        self.assertEqual(bytes(finished['image_digest']).hex(), '1' * 64)
        self.assertNotIn(source, str(events))
        self.assertTrue(self.audit.verify_trace(result['trace_id'])['valid'])

    def test_repeated_rejection_locks_sandbox_without_auto_reset(self):
        for _ in range(self.breaker.threshold):
            self.runner.execute('result = open("secret")')
        self.assertTrue(self.breaker.is_locked())
        with patch.object(self.runner, '_launch') as launch:
            result = self.runner.execute('result = 1 + 2')
        self.assertEqual(result['status'], 'LOCKED')
        launch.assert_not_called()

    def test_timeout_output_limit_oom_and_cleanup_failure_are_not_success(self):
        for status in ('TIMEOUT', 'OUTPUT_LIMIT', 'RESOURCE_LIMIT', 'CLEANUP_FAILED'):
            with self.subTest(status=status):
                runner = type(self.runner)(self.audit, ba.CircuitBreaker())
                with patch.object(runner, '_launch', return_value={'status': status, 'cleanup_ok': status != 'CLEANUP_FAILED'}):
                    result = runner.execute('result = 1')
                self.assertFalse(result['ok'])
                self.assertEqual(result['status'], status)
                self.assertEqual(runner.breaker.failures, 1)

    def test_control_checks_reject_individual_isolation_downgrades(self):
        from sandbox_execution import isolation_checks
        safe = {'Config': {'User': '65534:65534'}, 'Mounts': [], 'HostConfig': {
            'NetworkMode': 'none', 'ReadonlyRootfs': True, 'Privileged': False,
            'CapDrop': ['ALL'], 'SecurityOpt': ['no-new-privileges'], 'Memory': 67108864,
            'MemorySwap': 67108864, 'NanoCpus': 500000000, 'PidsLimit': 32,
            'Binds': None, 'PortBindings': {}, 'LogConfig': {'Type': 'none'}}}
        self.assertTrue(all(isolation_checks(safe).values()))
        for key, value in [('NetworkMode', 'bridge'), ('ReadonlyRootfs', False), ('Privileged', True),
                           ('CapDrop', []), ('SecurityOpt', []), ('Memory', 0), ('MemorySwap', -1),
                           ('NanoCpus', 0), ('PidsLimit', 0), ('Binds', ['/host:/app']),
                           ('PortBindings', {'80/tcp': [{}]}), ('LogConfig', {'Type': 'json-file'})]:
            altered = copy.deepcopy(safe)
            altered['HostConfig'][key] = value
            with self.subTest(key=key):
                self.assertFalse(all(isolation_checks(altered).values()))

    def test_unknown_probe_cannot_supply_arbitrary_code(self):
        with patch.object(self.runner, '_launch') as launch:
            result = self.runner.probe('import os; os.system("anything")')
        self.assertEqual(result['status'], 'REJECTED')
        launch.assert_not_called()

    def test_audit_failure_stops_before_container_execution(self):
        with patch.object(self.audit, 'write', side_effect=RuntimeError('audit unavailable')), \
             patch.object(self.runner, '_launch') as launch:
            with self.assertRaises(RuntimeError):
                self.runner.execute('result = 1 + 2')
        launch.assert_not_called()

    def test_malformed_inputs_are_rejected_and_audited(self):
        for source in (None, {}, 'result = 1 #\ud800'):
            runner = type(self.runner)(self.audit, ba.CircuitBreaker())
            with self.subTest(kind=type(source).__name__), patch.object(runner, '_launch') as launch:
                result = runner.execute(source)
                self.assertEqual(result['status'], 'REJECTED')
                self.assertTrue(self.audit.verify_trace(result['trace_id'])['valid'])
                launch.assert_not_called()

    def test_output_is_bounded_but_pipe_is_drained_to_avoid_client_deadlock(self):
        import io
        from types import SimpleNamespace
        payload = b'x' * (self.runner.max_output_bytes * 4)
        class Stream(io.BytesIO):
            drained = False
            def close(self):
                self.drained = self.tell() == len(payload)
                super().close()
        stream = Stream(payload)
        process = SimpleNamespace(stdout=stream, returncode=0, poll=lambda: 0, wait=lambda **kwargs: 0)
        with patch('sandbox_execution.subprocess.Popen', return_value=process):
            status, output, _ = self.runner._attached('owned-container')
        self.assertEqual(status, 'OUTPUT_LIMIT')
        self.assertLessEqual(len(output), self.runner.max_output_bytes)
        self.assertTrue(stream.drained)
