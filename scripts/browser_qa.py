#!/usr/bin/env python3
"""Install optional local browser QA tools and run isolated unittest scenarios.

python scripts/browser_qa.py --install  # explicit download, then four UI tests
python scripts/browser_qa.py            # reuse installed tools, four UI tests
python scripts/browser_qa.py --full     # full suite, including recovery drill
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import json
import locale
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
TOOLS = ROOT / '.qa-tools'
PLAYWRIGHT_VERSION = '1.64.1'
BROWSER_TESTS = [
    'test_banking_flow_ux.BankingFlowBrowserTests',
    'test_c7_elder_mode.ElderModeTests.test_browser_mode_toggle_fraud_and_customer_service',
    'test_voice_ui.VoiceBrowserTests',
]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Download pinned QA tools and Chromium into .qa-tools')
    parser.add_argument('--full', action='store_true', help='Run the full suite with UI and recovery drill enabled')
    args = parser.parse_args()
    node = shutil.which('node')
    if node is None:
        parser.error('Node.js is required for browser QA only')
    env = dict(os.environ, PLAYWRIGHT_BROWSERS_PATH=str(TOOLS / 'browsers'))
    if args.install:
        npm = shutil.which('npm.cmd' if os.name == 'nt' else 'npm')
        if npm is None:
            parser.error('npm is required to install optional QA tools')
        subprocess.run([npm, 'install', '--prefix', str(TOOLS), '--cache', str(TOOLS / 'npm-cache'),
                        '--ignore-scripts', '--no-audit', '--no-fund', '--save-exact',
                        f'playwright@{PLAYWRIGHT_VERSION}'], cwd=ROOT, env=env, check=True)
        subprocess.run([node, str(TOOLS / 'node_modules/playwright/cli.js'), 'install', 'chromium'],
                       cwd=ROOT, env=env, check=True)
    module = TOOLS / 'node_modules/playwright/index.js'
    if not module.is_file():
        parser.error('Playwright is not installed; run with --install first')
    package = json.loads((module.parent / 'package.json').read_text(encoding='utf-8'))
    if package['version'] != PLAYWRIGHT_VERSION:
        parser.error(f'Expected Playwright {PLAYWRIGHT_VERSION}; run with --install to align the tools')
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
    records = ROOT / 'data/verification' / f't03-{stamp}'
    records.mkdir(parents=True)
    database = ROOT / 'data/banking_agent.sqlite3'
    original_hash = hashlib.sha256(database.read_bytes()).hexdigest() if database.exists() else None
    env.update(C7_NODE_BINARY=node, C7_PLAYWRIGHT_MODULE=str(module),
               PYTHONDONTWRITEBYTECODE='1', C8_RUN_DRILL='1',
               PYTHONPATH=os.pathsep.join([str(ROOT / 'app'), str(ROOT / 'tests')]))
    env.pop('PYTHONUTF8', None)
    env.pop('PYTHONIOENCODING', None)
    env.pop('FLOW_SCREENSHOT_DIR', None)
    env.pop('C7_SCREENSHOT_DIR', None)
    command = [sys.executable, '-X', 'utf8=0', '-m', 'unittest']
    command += ['discover', '-s', 'tests', '-v'] if args.full else BROWSER_TESTS + ['-v']
    output_encoding = locale.getencoding()
    with tempfile.TemporaryDirectory(prefix='bank-agent-browser-qa-') as temporary:
        env['BANK_AGENT_DB_PATH'] = str(Path(temporary) / 'isolated-runtime.sqlite3')
        try:
            run = subprocess.run(command, cwd=ROOT, env=env, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, timeout=600)
            output, returncode = run.stdout.decode(output_encoding, errors='replace'), run.returncode
        except subprocess.TimeoutExpired as exc:
            output = (exc.stdout or b'').decode(output_encoding, errors='replace') + '\nQA TIMEOUT\n'
            returncode = 124
    count = re.search(r'^Ran (\d+) tests? in ', output, re.MULTILINE)
    skipped = re.search(r'\bskipped=(\d+)', output)
    resource_warnings = sum('ResourceWarning:' in line and 'Enable tracemalloc' not in line
                            for line in output.splitlines())
    after_hash = hashlib.sha256(database.read_bytes()).hexdigest() if database.exists() else None
    passed = (returncode == 0 and count is not None and skipped is None
              and resource_warnings == 0 and original_hash == after_hash)
    report = {'status': 'PASS' if passed else 'FAIL', 'full_suite': args.full,
              'scope': 'Mock bank browser QA; not real screen-reader or live ASR certification',
              'command': command, 'returncode': returncode,
              'tests_run': int(count[1]) if count else None, 'skipped': int(skipped[1]) if skipped else 0,
              'resource_warnings': resource_warnings,
              'playwright_version': package['version'], 'node_binary': node,
              'node_version': subprocess.check_output([node, '--version'], env=env).decode('ascii').strip(),
              'playwright_module': str(module), 'browser_path': env['PLAYWRIGHT_BROWSERS_PATH'],
              'python_utf8_mode': 0, 'recovery_drill_enabled': True,
              'subprocess_output_encoding': output_encoding,
              'original_database_unchanged': original_hash == after_hash, 'test_output': output}
    destination = records / 'report.json'
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(output, end='')
    print(f'QA status: {report["status"]}; report: {destination}')
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
