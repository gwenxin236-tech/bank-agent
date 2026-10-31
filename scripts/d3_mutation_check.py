#!/usr/bin/env python3
"""D3 isolated red/green checks; never mutate the live app or run external services."""
import argparse
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = ('test_production_contracts', 'test_business_logic_security', 'test_d1_security')
PC = 'test_production_contracts.ProductionContractTests.'
BL = 'test_business_logic_security.BusinessLogicSecurityTests.'
CO = 'test_business_logic_security.CoolingOffEnforcementTests.'
D1 = 'test_d1_security.D1SecurityTests.'


@dataclass(frozen=True)
class Mutation:
    name: str
    file: str
    before: str
    after: str
    test: str


def mutations():
    auth = PC + 'test_mock_strong_auth_binds_challenge_to_operation_digest'
    queue = PC + 'test_in_memory_queue_supports_message_and_dead_letter'
    store = PC + 'test_task_store_round_trip'
    return [
        Mutation('auth.digest_unbound', 'production_contracts.py',
                 'not hmac.compare_digest(operation_digest or "", item["operation_digest"])', 'False', auth),
        Mutation('auth.actor_unbound', 'production_contracts.py', 'ctx.actor_id != item["actor_id"]', 'False', auth),
        Mutation('auth.device_unbound', 'production_contracts.py', 'ctx.device_id != item["device_id"]', 'False', auth),
        Mutation('auth.empty_assertion', 'production_contracts.py', 'or not assertion):', 'or False):', auth),
        Mutation('auth.replay_allowed', 'production_contracts.py', 'if item["used"]:', 'if False:', auth),
        Mutation('auth.denial_consumes_challenge', 'production_contracts.py',
                 '            return AdapterResult(False, "AUTH_BINDING_MISMATCH",',
                 '            item["used"] = True\n            return AdapterResult(False, "AUTH_BINDING_MISMATCH",', auth),
        Mutation('core.denial_writes_ledger', 'production_contracts.py',
                 '            return AdapterResult(False, "INVALID_AMOUNT",',
                 '            self._transactions["unauthorized"] = {}\n            return AdapterResult(False, "INVALID_AMOUNT",',
                 PC + 'test_mock_bank_core_rejects_negative_float_bool_and_non_integer_amounts'),
        Mutation('queue.payload_lost', 'production_contracts.py',
                 'self.messages.append({"topic": topic, "payload": payload,',
                 'self.messages.append({"topic": topic, "payload": {},', queue),
        Mutation('queue.dead_letter_destroys_message', 'production_contracts.py',
                 '        self.dead_letters.append(', '        self.messages.clear()\n        self.dead_letters.append(', queue),
        Mutation('store.caller_alias', 'production_contracts.py',
                 'self._items[task_id] = dict(state)', 'self._items[task_id] = state', store),
        Mutation('store.update_noop', 'production_contracts.py',
                 'return self.put(task_id, state)', 'return self.get(task_id)', store),
        Mutation('backend.missing_dependency_no_guidance', 'production_backends.py',
                 '请安装 requirements-production.txt', '配置错误', PC + 'test_optional_backend_dependency_errors_are_explicit'),
        Mutation('backend.client_timeout_lost', 'production_backends.py',
                 'socket_timeout=2, socket_connect_timeout=2)\n        self.prefix = prefix\n        self.ttl_seconds',
                 'socket_timeout=20, socket_connect_timeout=2)\n        self.prefix = prefix\n        self.ttl_seconds',
                 PC + 'test_optional_backend_constructs_supplied_client_without_network'),
        Mutation('endpoint.route_missing', 'banking_agent.py',
                 'if path == "/api/production/contracts":', 'if path == "/api/production/contracts-disabled":',
                 'test_production_contracts.ProductionEndpointTests.test_production_contract_endpoint_returns_manifest'),
        Mutation('http.anonymous_read_allowed', 'family_access.py',
                 '        if not account:\n            if required or authorization:',
                 "        if not account:\n            account = {'user_id': 'demo-user', 'device_id': 'demo-device', 'role': 'user'}\n        if not account:\n            if required or authorization:", D1 + 'test_private_reads_require_session_including_audit_aliases'),
        Mutation('http.cross_user_allowed', 'family_access.py',
                 "elif actor != plan.actor_id or account['device_id'] != plan.device_id:", 'elif False:',
                 D1 + 'test_cross_user_confirm_verify_cancel_rejected_and_audited'),
        Mutation('http.denial_audit_missing', 'family_access.py',
                 "self.rt.audit.write(trace, 'authz.denied', {", "self.rt.audit.write(trace, 'wrong.event', {",
                 D1 + 'test_cross_user_confirm_verify_cancel_rejected_and_audited'),
        Mutation('http.denial_mutates_task', 'family_access.py',
                 "        context = plan.entities.get('_family', {}) if plan else {}",
                 "        if plan:\n            plan.entities['unauthorized_change'] = True\n        context = plan.entities.get('_family', {}) if plan else {}",
                 D1 + 'test_cross_user_confirm_verify_cancel_rejected_and_audited'),
        Mutation('subscription.silent_default_side_effect', 'banking_agent.py',
                 '            if not e.get("subscription_id"):\n',
                 '            if not e.get("subscription_id"):\n                self.bank.cancel_subscription("sub_001", "d3:unauthorized-default")\n',
                 D1 + 'test_missing_subscription_id_is_rejected_without_any_cancellation'),
        Mutation('subscription.denial_audit_missing', 'banking_agent.py',
                 'self.audit.write(plan.trace_id, "tool.denied", {"plan_id": plan.plan_id, "tool": step.tool, "reason": code}',
                 'self.audit.write(plan.trace_id, "wrong.event", {"plan_id": plan.plan_id, "tool": step.tool, "reason": code}',
                 D1 + 'test_missing_subscription_id_is_rejected_without_any_cancellation'),
        Mutation('business.rejection_audit_missing', 'banking_agent.py',
                 'self.audit.write(plan.trace_id, "business_input.blocked",',
                 'self.audit.write(plan.trace_id, "wrong.event",', BL + 'test_negative_amount_is_blocked_without_debit'),
        Mutation('business.old_token_audit_missing', 'banking_agent.py',
                 'self.audit.write(plan.trace_id, "consent.rejected",',
                 'self.audit.write(plan.trace_id, "wrong.event",', BL + 'test_old_token_cannot_execute_another_plan'),
        Mutation('business.toc_tou_recheck_ignored', 'banking_agent.py',
                 'if not recheck.ok:', 'if False:', BL + 'test_toc_tou_rechecks_balance_before_side_effect'),
        Mutation('business.expired_task_retained', 'banking_agent.py',
                 'if datetime.now(timezone.utc) >= datetime.fromisoformat(plan.token_expires_at):\n                    self.pending.pop(plan_id, None)',
                 'if datetime.now(timezone.utc) >= datetime.fromisoformat(plan.token_expires_at):\n                    pass',
                 'test_business_logic_security.AdditionalControlRegressionTests.test_expired_token_is_rejected'),
        Mutation('business.empty_audit_false_pass', 'banking_agent.py',
                 '"plan.created"', '"wrong.event"',
                 'test_business_logic_security.AdditionalControlRegressionTests.test_audit_never_persists_consent_token'),
        Mutation('cooling.failure_reported_as_success', 'banking_agent.py',
                 'return {"ok": False, "status": "FAILED", "steps": [asdict(s) for s in plan.steps], "error": result.error_message, "trace_id": plan.trace_id}',
                 'return {"ok": False, "status": "SUCCESS", "steps": [asdict(s) for s in plan.steps], "error": result.error_message, "trace_id": plan.trace_id}',
                 CO + 'test_cooling_off_blocks_later_transfer_to_same_beneficiary'),
        Mutation('cooling.active_guard_removed', 'banking_agent.py',
                 'for case in self.cooling_off_cases.values():', 'for case in ():',
                 CO + 'test_cooling_off_blocks_later_transfer_to_same_beneficiary'),
        Mutation('cooling.expired_review_bypassed', 'banking_agent.py',
                 '                    case["status"] = "EXPIRED_PENDING_REVIEW"\n                except',
                 '                    continue\n                except',
                 CO + 'test_expired_cooling_off_still_requires_manual_review_via_agent'),
        Mutation('cooling.failure_audit_missing', 'banking_agent.py',
                 'self.audit.write(plan.trace_id, "tool.failed",',
                 'self.audit.write(plan.trace_id, "wrong.event",',
                 CO + 'test_cooling_off_blocks_later_transfer_to_same_beneficiary'),
        Mutation('cooling.queried_status_audit_missing', 'banking_agent.py',
                 '                            "transaction.status.queried",',
                 '                            "wrong.event",',
                 CO + 'test_cooling_off_blocks_later_transfer_to_same_beneficiary'),
    ]


WORKER = r'''
import io, json, sys, unittest
stream = io.StringIO()
suite = unittest.defaultTestLoader.loadTestsFromNames(sys.argv[1:])
result = unittest.TextTestRunner(stream=stream, verbosity=2).run(suite)
print(json.dumps({"tests_run": result.testsRun,
                  "failures": [{"test": str(t), "detail": text} for t, text in result.failures],
                  "errors": [{"test": str(t), "detail": text} for t, text in result.errors],
                  "skipped": result.skipped, "successful": result.wasSuccessful(),
                  "log": stream.getvalue()}))
'''


def run_tests(work, names):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE='1', BANK_AGENT_OFFLINE='1',
               BANK_AGENT_DB_PATH=str(work / 'scratch.sqlite3'),
               PYTHONPATH=os.pathsep.join([str(work / 'app'), str(work / 'tests')]))
    completed = subprocess.run([sys.executable, '-c', WORKER, *names], cwd=work, env=env,
                               capture_output=True, text=True, timeout=90)
    if completed.returncode:
        raise RuntimeError(f'Worker exit {completed.returncode}: {completed.stderr}')
    result = json.loads(completed.stdout)
    result['stderr'] = completed.stderr
    return result


def green(result):
    return result['tests_run'] > 0 and result['successful'] and not result['skipped']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report', type=Path, help='Optional JSON evidence path, preferably outside repository')
    args = parser.parse_args()
    cases = mutations()
    # Fingerprint the exact workspace under test, including existing uncommitted files.
    inputs = sorted(p for directory in ('app', 'config', 'eval')
                    for p in (ROOT / directory).rglob('*')
                    if p.is_file() and '__pycache__' not in p.parts and p.suffix != '.pyc')
    inputs += [ROOT / 'tests' / (m + '.py') for m in MODULES]
    report = {'scope': 'workspace snapshot; targeted invariants, not exhaustive mutation coverage',
              'sha256': {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest() for p in inputs},
              'mutations': [], 'pass': False}
    try:
        with tempfile.TemporaryDirectory(prefix='bank-d3-') as folder:
            work = Path(folder)
            for directory in ('app', 'config', 'eval'):
                shutil.copytree(ROOT / directory, work / directory,
                                ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
            (work / 'tests').mkdir()
            for module in MODULES:
                shutil.copy2(ROOT / 'tests' / (module + '.py'), work / 'tests' / (module + '.py'))
            baseline = run_tests(work, MODULES)
            report['baseline'] = baseline
            if not green(baseline):
                raise RuntimeError('Green control failed; no mutation result is valid: ' + baseline['log'])
            print(f"GREEN baseline: {baseline['tests_run']} tests", flush=True)
            for case in cases:
                path = work / 'app' / case.file
                original = path.read_text(encoding='utf-8')
                if original.count(case.before) != 1:
                    raise RuntimeError(f'{case.name}: stale or non-unique source anchor')
                try:
                    path.write_text(original.replace(case.before, case.after), encoding='utf-8')
                    result = run_tests(work, [case.test])
                finally:
                    path.write_text(original, encoding='utf-8')
                # Import errors, unexpected exceptions, timeouts and skips are NOT kills.
                killed = (result['tests_run'] == 1 and bool(result['failures'])
                          and not result['errors'] and not result['skipped'])
                report['mutations'].append({'name': case.name, 'source': case.file,
                                            'test': case.test, 'killed': killed, 'result': result})
                print(f"{'PASS red' if killed else 'FAIL survivor/harness'}: {case.name}", flush=True)
            report['restored'] = run_tests(work, MODULES)
            report['pass'] = (all(m['killed'] for m in report['mutations'])
                              and len(report['mutations']) == len(cases) and green(report['restored']))
            print(f"{'PASS' if green(report['restored']) else 'FAIL'} restored green: "
                  f"{report['restored']['tests_run']} tests", flush=True)
    except (OSError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        report['harness_error'] = str(exc)
        print('FAIL harness: ' + str(exc), file=sys.stderr)
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(f"{'PASS' if report['pass'] else 'FAIL'} D3: "
          f"{sum(m['killed'] for m in report['mutations'])}/{len(cases)} assertion-detected mutations")
    return 0 if report['pass'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
