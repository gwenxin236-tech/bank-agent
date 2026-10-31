#!/usr/bin/env python3
"""Run the T07 operator-only isolated demo using cached python:3.12-slim.

No arbitrary HTTP/code endpoint, image pull, host mounts or production banking
inputs. Raw container output and submitted code are not written to the audit.
"""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))


def main():
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
    records = ROOT / 'data/verification' / ('t07-docker-' + stamp)
    records.mkdir(parents=True)
    original = ROOT / 'data/banking_agent.sqlite3'
    fingerprint = lambda: hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None
    before = fingerprint()
    report = {'status': 'FAIL', 'scope': 'LOCAL_ISOLATED_DEMO; synthetic data; not production isolation certification', 'cases': []}
    previous = {k: os.environ.get(k) for k in ('BANK_AGENT_DB_PATH', 'T07_HOST_SENTINEL')}
    try:
        with tempfile.TemporaryDirectory(prefix='bank-sandbox-qa-') as temporary:
            os.environ['BANK_AGENT_DB_PATH'] = str(Path(temporary) / 'import.sqlite3')
            os.environ['T07_HOST_SENTINEL'] = 'synthetic-local-test-marker'
            import banking_agent as ba
            from sandbox_execution import SandboxRunner
            audit = None
            try:
                audit = ba.AuditStore(records / 'audit.sqlite3')

                def exercise(name, source, expected, probe=False):
                    print('STEP:', name, flush=True)
                    runner = SandboxRunner(audit, ba.CircuitBreaker())
                    result = runner.probe(source) if probe else runner.execute(source)
                    verified = audit.verify_trace(result['trace_id'])
                    passed = (result['status'] == expected and result.get('cleanup_ok', True)
                              and verified['valid'] and verified['count'] > 0)
                    if name == 'calculation':
                        passed = passed and result.get('data', {}).get('result') == 10200
                    report['cases'].append({'name': name, 'expected': expected, 'passed': passed,
                                            'result': result, 'audit_verify': verified})
                    print('RESULT:', name, result['status'], 'PASS' if passed else 'FAIL', flush=True)
                    if not passed:
                        raise RuntimeError('sandbox acceptance failed: ' + name)

                exercise('calculation', 'result = sum([1200, 3400, 5600])', 'SUCCESS')
                exercise('rejected_import', 'result = __import__("os").getcwd()', 'REJECTED')
                exercise('runtime_error', 'result = 1 // 0', 'FAILED')
                exercise('environment', 'environment', 'SUCCESS', probe=True)
                exercise('timeout', 'timeout', 'TIMEOUT', probe=True)
                exercise('output_limit', 'output', 'OUTPUT_LIMIT', probe=True)
                exercise('memory_limit', 'memory', 'RESOURCE_LIMIT', probe=True)
                runner = SandboxRunner(audit, ba.CircuitBreaker())
                attempts = [runner.execute('result = open("secret")') for _ in range(runner.breaker.threshold)]
                blocked = runner.execute('result = 1 + 2')
                traces = [audit.verify_trace(r['trace_id']) for r in attempts + [blocked]]
                passed = blocked['status'] == 'LOCKED' and all(t['valid'] and t['count'] > 0 for t in traces)
                report['cases'].append({'name': 'breaker', 'passed': passed, 'result': blocked, 'audit_verify': traces})
                report['status'] = 'PASS' if passed else 'FAIL'
            finally:
                if audit is not None:
                    audit.db.close()
                ba.runtime.recovery_scheduler.stop()
                ba.runtime.bank.close()
                ba.runtime.audit.db.close()
    except Exception as error:
        report['status'] = 'FAIL'
        report['error'] = str(error) if isinstance(error, RuntimeError) else type(error).__name__
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        report['original_database_unchanged'] = fingerprint() == before
        if not report['original_database_unchanged']:
            report['status'] = 'FAIL'
        destination = records / 'report.json'
        destination.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print('Sandbox QA status:', report['status'], '; report:', str(destination), flush=True)
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
