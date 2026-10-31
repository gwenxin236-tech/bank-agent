#!/usr/bin/env python3
"""Loopback-only local operator client and isolated HTTP acceptance."""
import argparse
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
from pathlib import Path
import secrets
import sys
import tempfile
import threading
from urllib.parse import urlparse
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'app'))


def request(url, path, body, token):
    target = urlparse(url)
    if target.scheme != 'http' or target.hostname not in {'127.0.0.1', 'localhost', '::1'} or target.username or target.password or target.path not in {'', '/'} or target.query or target.fragment:
        raise ValueError('Only an explicit local loopback HTTP service is allowed')
    connection = http.client.HTTPConnection(target.hostname, target.port or 80, timeout=10)
    try:
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = 'Bearer ' + token
        connection.request('POST', path, json.dumps(body).encode('ascii'), headers)
        response = connection.getresponse()
        # http.client does not follow redirects or forward credentials elsewhere.
        return response.status, json.loads(response.read())
    finally:
        connection.close()


def acceptance():
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime('%Y%m%d-%H%M%S-%f')
    record = ROOT / 'data/verification' / ('t09-' + stamp) / 'report.json'
    record.parent.mkdir(parents=True)
    original = ROOT / 'data/banking_agent.sqlite3'
    fingerprint = lambda: hashlib.sha256(original.read_bytes()).hexdigest() if original.exists() else None
    before = fingerprint()
    previous = os.environ.get('BANK_AGENT_DB_PATH')
    report = {'status': 'FAIL', 'scope': 'LOCAL_DEMO_ONLY; no real agent or bank settlement', 'checks': []}
    with tempfile.TemporaryDirectory(prefix='bank-handoff-qa-') as temporary:
        os.environ['BANK_AGENT_DB_PATH'] = str(Path(temporary) / 'import.sqlite3')
        import banking_agent as ba
        rt = ba.AgentRuntime(db_path=Path(temporary) / 'cases.sqlite3', offline=True)
        operator_token = secrets.token_urlsafe(32)
        rt.family.demo_sessions[operator_token] = {'user_id': 'operator', 'device_id': 'ops-device', 'role': 'admin'}
        class QuietHandler(ba.Handler):
            def log_message(self, *args):
                pass
        def check(name, passed):
            report['checks'].append({'name': name, 'passed': bool(passed)})
            if not passed:
                raise AssertionError(name)
        try:
            with patch.object(ba, 'runtime', rt), patch.object(ba, 'gateway', ba.AgentGateway(rt)), patch.object(ba.http_rate_limiter, 'allow', return_value=True):
                with ba.ThreadingHTTPServer(('127.0.0.1', 0), QuietHandler) as server:
                    thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
                    thread.start()
                    try:
                        url = 'http://127.0.0.1:' + str(server.server_port)
                        call = lambda action, body, token=operator_token: request(url, '/api/v1/handoff/' + action, body, token)
                        check('anonymous_denied', call('inbox', {}, '')[0] == 403)
                        check('user_cannot_be_operator', call('inbox', {}, 'demo-user-session')[0] == 403)
                        code, queued = request(url, '/api/v1/customer-service', {'request_id': 'qa-support'}, 'demo-user-session')
                        case = queued['handoff_case']
                        report['case_id'], report['trace_id'] = case['case_id'], case['trace_id']
                        body = {'case_id': case['case_id']}
                        check('queued', code == 200 and case['status'] == 'LOCAL_QUEUED')
                        check('inbox_has_redacted_card', call('inbox', {})[1]['items'][0]['case_id'] == case['case_id'])
                        check('cannot_close_before_claim', call('resolve', dict(body, resolution='REFERRED_OFFICIAL'))[0] == 409)
                        claimed = call('claim', body)
                        check('operator_claim', claimed[0] == 200 and claimed[1]['case']['status'] == 'LOCAL_CLAIMED')
                        check('claim_idempotent', call('claim', body) == claimed)
                        check('user_observes_claim', call('status', body, 'demo-user-session')[1]['case']['status'] == 'LOCAL_CLAIMED')
                        check('cannot_fabricate_bank_success', call('resolve', dict(body, resolution='TRANSFER_SUCCESS'))[0] == 400)
                        closed = call('resolve', dict(body, resolution='REFERRED_OFFICIAL'))
                        check('operator_closes_local_case', closed[0] == 200 and closed[1]['case']['status'] == 'LOCAL_CLOSED')
                        check('closure_idempotent', call('resolve', dict(body, resolution='REFERRED_OFFICIAL')) == closed)
                        check('user_observes_conclusion', call('status', body, 'demo-user-session')[1]['case']['resolution'] == 'REFERRED_OFFICIAL')
                        repeated = request(url, '/api/v1/customer-service', {'request_id': 'qa-support'}, 'demo-user-session')[1]
                        check('request_does_not_reopen_closed_case', repeated['handoff_case']['case_id'] == case['case_id'] and repeated['handoff_case']['status'] == 'LOCAL_CLOSED')
                    finally:
                        server.shutdown()
                        thread.join(timeout=5)
            proof = rt.audit.verify_trace(case['trace_id'])
            report['audit_verify'] = proof
            check('valid_nonempty_audit', proof['valid'] and proof['count'] > 0)
            check('no_bank_side_effect', rt.bank.daily_transfer_cents == 0 and rt.bank.balance_cents == rt.bank.INITIAL_BALANCE_CENTS)
        except Exception as error:
            report['error'] = type(error).__name__ + ': ' + str(error)
        finally:
            for instance in (rt, ba.runtime):
                instance.recovery_scheduler.stop()
                instance.bank.close()
                instance.audit.db.close()
            if previous is None:
                os.environ.pop('BANK_AGENT_DB_PATH', None)
            else:
                os.environ['BANK_AGENT_DB_PATH'] = previous
        report['original_database_unchanged'] = before == fingerprint()
        report['status'] = 'PASS' if not report.get('error') and report['original_database_unchanged'] else 'FAIL'
        record.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        print(str(record), report['status'])
        return 0 if report['status'] == 'PASS' else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--qa', action='store_true')
    parser.add_argument('--url', default='http://127.0.0.1:8090')
    parser.add_argument('--action', choices=['inbox', 'claim', 'resolve'], default='inbox')
    parser.add_argument('--case-id')
    parser.add_argument('--resolution', choices=['GUIDANCE_PROVIDED', 'REFERRED_OFFICIAL', 'NEEDS_RECONCILIATION'])
    parser.add_argument('--offset', type=int, default=0)
    args = parser.parse_args()
    if args.qa:
        return acceptance()
    token = os.getenv('BANK_AGENT_DEMO_ADMIN_TOKEN', '').strip()
    if not token:
        parser.error('Configure BANK_AGENT_DEMO_ADMIN_TOKEN in the trusted service and operator shell; never paste it into chat or browser code')
    body = {'offset': args.offset} if args.action == 'inbox' else {'case_id': args.case_id}
    if args.action != 'inbox' and not args.case_id:
        parser.error('--case-id is required')
    if args.action == 'resolve':
        if not args.resolution:
            parser.error('--resolution is required')
        body['resolution'] = args.resolution
    try:
        status, result = request(args.url, '/api/v1/handoff/' + args.action, body, token)
        print(json.dumps(result, ensure_ascii=True))
        return 0 if status == 200 and result.get('ok') else 1
    except Exception:
        print('Local case result unavailable; inspect the original case. No automatic retry.', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
