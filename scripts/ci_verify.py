#!/usr/bin/env python3
"""Strict offline CI: isolated DB, declared browser partition, no silent skips.

core/browser are complementary jobs; full selects all tests. No paid API calls.
Reports are evidence for this machine, not proof of GitHub Actions execution.
"""
import argparse
import gc
import hashlib
import inspect
import json
import locale
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import warnings

ROOT = Path(__file__).resolve().parents[1]


def digest(path): return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def leaves(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite): yield from leaves(test)
        else: yield test


def select(tests, scope):
    if scope not in ('core', 'browser', 'full'): raise ValueError('unknown scope')
    selected, excluded = [], []
    for test in tests:
        try: source = inspect.getsource(getattr(test, test._testMethodName))
        except (AttributeError, OSError, TypeError): source = ''
        browser = 'C7_PLAYWRIGHT_MODULE' in source
        if scope == 'full' or (scope == 'browser') == browser: selected.append(test)
        else: excluded.append({'id': test.id(), 'reason': 'separate browser job' if browser else 'separate core job'})
    return selected, excluded


def acceptable(success, count, skipped, resource_warnings, unchanged):
    return bool(success and count > 0 and skipped == 0 and resource_warnings == 0 and unchanged)


def partition(tests, index, count):
    """Balance modules deterministically; never split module/class fixtures."""
    if not 1 <= count <= 16 or not 0 <= index < count:
        raise ValueError('invalid CI shard')
    modules = {}
    for test in tests:
        modules.setdefault(test.__class__.__module__, []).append(test)
    loads, owners = [0] * count, {}
    for module in sorted(modules, key=lambda name: (-len(modules[name]), name)):
        owner = min(range(count), key=lambda i: (loads[i], i))
        owners[module] = owner
        loads[owner] += len(modules[module])
    return [test for test in tests if owners[test.__class__.__module__] == index]


def run_suite(suite, output, stall_seconds=0):
    """A stalled CI worker fails; diagnostics contain locations, never locals."""
    output = Path(output)
    progress = output.with_suffix('.progress.jsonl')
    lock, stop = threading.Lock(), threading.Event()
    state = {'active': None, 'started': 0.0, 'completed': 0}

    class Result(unittest.TextTestResult):
        def startTest(self, test):
            with lock:
                state.update(active=test.id(), started=time.monotonic())
            super().startTest(test)

        def stopTest(self, test):
            with lock:
                elapsed = time.monotonic() - state['started']
                state['completed'] += 1
                state['active'] = None
                with progress.open('a', encoding='utf-8') as stream:
                    stream.write(json.dumps({'test': test.id(), 'seconds': round(elapsed, 4)}) + '\n')
            super().stopTest(test)

    def watch():
        while not stop.wait(min(1.0, stall_seconds / 4)):
            with lock:
                elapsed = time.monotonic() - state['started']
                if state['active'] is None or elapsed < stall_seconds:
                    continue
                threads = []
                for identifier, frame in sys._current_frames().items():
                    frames = []
                    while frame:
                        frames.append({'file': frame.f_code.co_filename,
                                       'line': frame.f_lineno, 'function': frame.f_code.co_name})
                        frame = frame.f_back
                    threads.append({'id': identifier, 'frames': frames})
                report = {'status': 'FAIL', 'error': 'single test made no completion progress',
                          'active_test': state['active'], 'elapsed_seconds': elapsed,
                          'completed_tests': state['completed'], 'threads': threads}
                output.write_text(json.dumps(report, ensure_ascii=True, indent=2), encoding='utf-8')
                # Only the isolated test worker is stopped, never a bank process.
                os._exit(124)

    thread = None
    if stall_seconds > 0:
        thread = threading.Thread(target=watch, name='ci-test-watchdog', daemon=True)
        thread.start()
    try:
        return unittest.TextTestRunner(verbosity=2, resultclass=Result).run(suite)
    finally:
        stop.set()
        if thread is not None:
            thread.join(timeout=2)


def safe_environment(original, database):
    env = dict(original)
    for key in list(env):
        if key.endswith(('_API_KEY', '_ACCESS_TOKEN')) or key in ('BANK_AGENT_DEMO_ADMIN_TOKEN', 'PYTHONUTF8', 'PYTHONIOENCODING', 'FLOW_SCREENSHOT_DIR', 'C7_SCREENSHOT_DIR'):
            env.pop(key, None)
    env.update(BANK_AGENT_DB_PATH=str(database), BANK_AGENT_OFFLINE='1', IDENTITY_MODE='demo',
               APP_ENV='demo', C8_RUN_DRILL='1', PYTHONDONTWRITEBYTECODE='1',
               OCR_ENABLED='1', RAG_USE_RERANKER='0', RAG_USE_MODEL='0', VOICE_CLOUD_ENABLED='0')
    env['PYTHONPATH'] = os.pathsep.join([str(ROOT / '.qa-tools/local-ocr/windows-site'), str(ROOT / 'app'), str(ROOT / 'tests')])
    return env


def worker(scope, output, shard_index=0, shard_count=1):
    sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'tests'), str(ROOT / 'scripts')]
    daily = ROOT / 'data/banking_agent.sqlite3'; before = digest(daily)
    source_files = {p.relative_to(ROOT).as_posix(): digest(p) for directory in ('app', 'tests', 'scripts', 'config')
                    for p in (ROOT / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                    and p.suffix in ('.py', '.js', '.html', '.css', '.json', '.svg')}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', ResourceWarning)
        discovered = list(leaves(unittest.defaultTestLoader.discover(str(ROOT / 'tests'))))
        selected, excluded = select(discovered, scope)
        scope_total = len(selected)
        selected = partition(selected, shard_index, shard_count)
        stall = int(os.getenv('BANK_CI_TEST_TIMEOUT_SECONDS', '0'))
        if not 0 <= stall <= 900:
            raise ValueError('invalid CI test timeout')
        result = run_suite(unittest.TestSuite(selected), output, stall)
        module = sys.modules.get('banking_agent')
        if module is not None:
            module.runtime.recovery_scheduler.stop()
            module.runtime.bank.close()
            module.runtime.audit.db.close()
        gc.collect()
        leaks = [w for w in caught if issubclass(w.category, ResourceWarning)]
    unchanged = before == digest(daily)
    source_unchanged = all(digest(ROOT / name) == value for name, value in source_files.items())
    report = {'schema': 1, 'status': 'PASS' if acceptable(result.wasSuccessful(), result.testsRun, len(result.skipped), len(leaks), unchanged and source_unchanged) else 'FAIL',
              'scope': scope, 'tests_run': result.testsRun, 'discovered': len(discovered),
              'shard_index': shard_index, 'shard_count': shard_count, 'scope_total': scope_total,
              'selected_tests': [test.id() for test in partition(select(discovered, scope)[0], shard_index, shard_count)],
              'skipped': len(result.skipped), 'resource_warnings': len(leaks), 'excluded': excluded,
              'failures': len(result.failures), 'errors': len(result.errors),
              'original_database_unchanged': unchanged, 'sources_unchanged': source_unchanged,
              'source_files': source_files, 'python': sys.version.split()[0], 'platform': sys.platform,
              'cloud_calls': 0, 'remote_ci_execution': False}
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    return 0 if report['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scope', choices=('core', 'browser', 'full'), default='core')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--worker', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--shard-index', type=int, default=0)
    parser.add_argument('--shard-count', type=int, default=1)
    args = parser.parse_args(); output = args.output.resolve()
    if not 1 <= args.shard_count <= 16 or not 0 <= args.shard_index < args.shard_count:
        parser.error('invalid CI shard')
    if output.exists(): parser.error('fresh report path required')
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker: return worker(args.scope, output, args.shard_index, args.shard_count)
    with tempfile.TemporaryDirectory(prefix='bank-ci-') as tmp:
        env = safe_environment(os.environ, Path(tmp) / 'isolated.sqlite3')
        command = [sys.executable]
        if os.name == 'nt': command += ['-X', 'utf8=0']
        command += [str(Path(__file__).resolve()), '--worker', '--scope', args.scope, '--output', str(output),
                    '--shard-index', str(args.shard_index), '--shard-count', str(args.shard_count)]
        try:
            result = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=900)
            log = result.stdout.decode(locale.getencoding(), errors='replace')
            output.with_suffix('.log').write_text(log, encoding='utf-8')
            print(log[-4000:]); print('CI report:', str(output))
            return result.returncode
        except subprocess.TimeoutExpired as exc:
            output.write_text(json.dumps({'status': 'FAIL', 'error': '900 second timeout', 'scope': args.scope}), encoding='utf-8')
            output.with_suffix('.log').write_bytes(exc.stdout or b'')
            return 1


if __name__ == '__main__': raise SystemExit(main())
