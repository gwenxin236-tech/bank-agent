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


def worker(scope, output):
    sys.path[:0] = [str(ROOT / 'app'), str(ROOT / 'tests'), str(ROOT / 'scripts')]
    daily = ROOT / 'data/banking_agent.sqlite3'; before = digest(daily)
    source_files = {p.relative_to(ROOT).as_posix(): digest(p) for directory in ('app', 'tests', 'scripts', 'config')
                    for p in (ROOT / directory).rglob('*') if p.is_file() and '__pycache__' not in p.parts
                    and p.suffix in ('.py', '.js', '.html', '.css', '.json', '.svg')}
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter('always', ResourceWarning)
        discovered = list(leaves(unittest.defaultTestLoader.discover(str(ROOT / 'tests'))))
        selected, excluded = select(discovered, scope)
        result = unittest.TextTestRunner(verbosity=2).run(unittest.TestSuite(selected))
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
    args = parser.parse_args(); output = args.output.resolve()
    if output.exists(): parser.error('fresh report path required')
    output.parent.mkdir(parents=True, exist_ok=True)
    if args.worker: return worker(args.scope, output)
    with tempfile.TemporaryDirectory(prefix='bank-ci-') as tmp:
        env = safe_environment(os.environ, Path(tmp) / 'isolated.sqlite3')
        command = [sys.executable]
        if os.name == 'nt': command += ['-X', 'utf8=0']
        command += [str(Path(__file__).resolve()), '--worker', '--scope', args.scope, '--output', str(output)]
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
