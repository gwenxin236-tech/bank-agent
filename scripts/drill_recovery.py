#!/usr/bin/env python3
"""Offline C8 SIGKILL/restart drill; owns its processes, loopback port and temp DB.

No service already running is contacted or killed. No production/demo DB is used.
Only the stopped, private database gets auth-expiry fault injection.
"""
import argparse
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.request import build_opener, ProxyHandler, Request

ROOT = Path(__file__).resolve().parents[1]


class DrillFailure(Exception):
    pass


class RecoveryDrill:
    def __init__(self, directory, rto_target):
        self.directory = Path(directory)
        self.database = self.directory / 'drill.sqlite3'
        self.rto_target = rto_target
        self.process = None
        self.log = None
        self.generation = 0
        self.base = ''
        self.checks = []
        self.rto = []
        self.opener = build_opener(ProxyHandler({}))  # never send loopback traffic to a proxy

    def check(self, name, condition):
        self.checks.append({'check': name, 'status': 'PASS' if condition else 'FAIL'})
        if not condition:
            raise DrillFailure(name)

    def request(self, path, body=None):
        request = Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                          # D1: fixed offline identity survives restart; production uses a renewed IAM session.
                          headers={'Content-Type': 'application/json', 'Authorization': 'Bearer demo-user-session'})
        try:
            with self.opener.open(request, timeout=2) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def post(self, path, body):
        code, out = self.request(path, body)
        self.check('http_' + path.rsplit('/', 1)[-1], code == 200)
        return out

    def start(self, recovery_started=None):
        self.generation += 1
        log_path = self.directory / f'server-{self.generation}.log'
        self.log = log_path.open('wb')
        env = dict(os.environ, BANK_AGENT_HOST='127.0.0.1', BANK_AGENT_PORT='0',
                   BANK_AGENT_DB_PATH=str(self.database), BANK_AGENT_OFFLINE='1', PYTHONUNBUFFERED='1')
        self.process = subprocess.Popen([sys.executable, str(ROOT / 'app/banking_agent.py')],
                                        cwd=str(ROOT), env=env, stdin=subprocess.DEVNULL,
                                        stdout=self.log, stderr=self.log)
        started = recovery_started if recovery_started is not None else time.monotonic()
        deadline = started + self.rto_target
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise DrillFailure('child_exited_before_ready')
            # Service binds port 0 itself: no reserve/release port race.
            match = re.search(r'running at (http://127\.0\.0\.1:\d+)', log_path.read_text(encoding='utf-8', errors='replace'))
            if match:
                self.base = match.group(1)
                try:
                    live_code, _ = self.request('/healthz')
                    ready_code, ready = self.request('/readyz')
                    if live_code == ready_code == 200 and ready.get('ok'):
                        self.check('ready_database_and_scheduler', all(ready['checks'].values()))
                        self.check('demo_rto_within_target', time.monotonic() - started <= self.rto_target)
                        if recovery_started is not None:
                            self.rto.append(round(time.monotonic() - started, 3))
                        return
                except (URLError, TimeoutError, OSError):
                    pass
            time.sleep(.05)
        raise DrillFailure('demo_rto_target_exceeded')

    def kill_owned_process(self):
        started = time.monotonic()
        if self.process is None or self.process.poll() is not None:
            raise DrillFailure('child_not_alive_at_crash')
        self.process.kill()  # SIGKILL on the supported Unix demo host; no process-name kill.
        self.process.wait(timeout=5)
        self.log.close()
        self.log = None
        return started

    def close(self):
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.log:
            self.log.close()

    def task(self, amount):
        out = self.post('/api/chat', {'message': f'给妈妈转 {amount} 元'})
        self.check('task_acknowledged_waiting', out.get('task_status') == 'waiting_confirmation')
        plan = out['plan']
        return {'plan_id': plan['plan_id'], 'task_id': plan['plan_id'],
                'action_token': plan['consent_token'], 'user_id': plan['actor_id'],
                'device_id': plan['device_id']}, plan['trace_id']

    def status(self, body):
        return self.post('/api/v1/tasks/status', body)

    def core_totals(self):
        with closing(sqlite3.connect(self.database)) as db:
            count, cents = db.execute('SELECT count(*), coalesce(sum(amount_cents * debit_applied),0) '
                                     'FROM core_transactions').fetchone()
        return count, cents

    def expire_private_credential(self, task_id, credential_id):
        # Deterministic five-minute expiry simulation; never alter the system clock.
        with closing(sqlite3.connect(self.database)) as db, db:
            row = db.execute('SELECT payload_json FROM task_checkpoints WHERE task_id=?', (task_id,)).fetchone()
            payload = json.loads(row[0])
            matches = [item for item in payload['credentials'] if item['credential_id'] == credential_id]
            self.check('private_expiry_injection_target', len(matches) == 1)
            matches[0]['expires_at'] = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
            db.execute('UPDATE task_checkpoints SET payload_json=? WHERE task_id=?',
                       (json.dumps(payload, ensure_ascii=False), task_id))

    def run(self):
        self.start()
        first, first_trace = self.task(100)
        self.check('first_waiting_before_crash', self.status(first)['status'] == 'waiting_confirmation')
        crash = self.kill_owned_process()
        self.start(crash)
        self.check('first_waiting_after_restart', self.status(first)['status'] == 'waiting_confirmation')
        time.sleep(.6)  # multiple scheduler ticks must not cross the confirmation node
        self.check('scheduler_did_not_execute_waiting', self.core_totals() == (0, 0))
        with ThreadPoolExecutor(max_workers=2) as pool:
            replies = list(pool.map(lambda _: self.request('/api/confirm', first)[1], range(2)))
        self.check('double_confirm_only_one_success',
                   sum(out.get('result', {}).get('status') == 'SUCCESS' for out in replies) == 1)
        self.check('first_final_status_queried', self.status(first)['status'] == 'succeeded')
        self.check('first_single_debit', self.core_totals() == (1, 10000))

        high, high_trace = self.task(5000)
        challenge = self.post('/api/confirm', high)
        old_id = challenge['auth_challenge']['credential_id']
        verified = self.post('/api/verify', dict(high, code='123456', credential_id=old_id))
        self.check('high_risk_authenticated_before_crash', verified.get('ok'))
        crash = self.kill_owned_process()
        self.expire_private_credential(high['task_id'], old_id)
        self.start(crash)
        self.check('high_risk_restored_waiting', self.status(high)['status'] == 'waiting_confirmation')
        self.check('no_high_risk_auto_execution', self.core_totals() == (1, 10000))
        old = self.post('/api/confirm', dict(high, credential_id=old_id))
        self.check('expired_credential_rejected', old.get('error_code') == 'AUTH_EXPIRED')
        fresh = self.post('/api/confirm', high)
        new_id = fresh['auth_challenge']['credential_id']
        self.check('expired_auth_rechallenged', fresh.get('needs_auth') and new_id != old_id)
        self.check('fresh_auth_required_before_debit', self.core_totals() == (1, 10000))
        self.check('fresh_auth_passed', self.post('/api/verify', dict(high, code='123456', credential_id=new_id))['ok'])
        completed = self.post('/api/confirm', dict(high, credential_id=new_id))
        self.check('high_risk_confirm_completed', completed.get('result', {}).get('status') == 'SUCCESS')
        self.check('high_risk_final_status_queried', self.status(high)['status'] == 'succeeded')
        replay = self.post('/api/confirm', dict(high, credential_id=new_id))
        self.check('credential_replay_rejected', replay.get('error_code') == 'AUTH_REPLAY')
        self.check('no_duplicate_side_effects', self.core_totals() == (2, 510000))
        for trace in (first_trace, high_trace):
            code, result = self.request('/api/v1/audit/verify?trace_id=' + trace)
            self.check('audit_chain_valid', code == 200 and result.get('valid') is True)
        # Restart again after both completions; old confirmations must remain unusable.
        crash = self.kill_owned_process()
        self.start(crash)
        self.check('completed_task_still_succeeded', self.status(first)['status'] == 'succeeded')
        self.check('completed_confirm_not_replayed', not self.post('/api/confirm', first).get('ok'))
        self.check('used_credential_survives_restart',
                   self.post('/api/confirm', dict(high, credential_id=new_id)).get('error_code') == 'AUTH_REPLAY')
        self.check('post_restart_no_duplicate_side_effects', self.core_totals() == (2, 510000))
        with closing(sqlite3.connect(self.database)) as db:
            grants = db.execute("SELECT count(*) FROM audit_events WHERE event_type='consent.granted'").fetchone()[0]
        self.check('exactly_two_confirmation_grants', grants == 2)
        return {'status': 'PASS', 'scope': 'demo_only_not_production_dr_acceptance',
                'rpo_acknowledged_waiting_tasks_lost': 0, 'rto_target_seconds': self.rto_target,
                'rto_observed_seconds': self.rto, 'core_transaction_count': 2,
                'debit_total_cents': 510000, 'expired_auth_rechallenged': True, 'checks': self.checks}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--rto-target-seconds', type=float, default=30,
                        help='Demonstration restart-to-ready deadline (default 30 seconds)')
    args = parser.parse_args()
    if not math.isfinite(args.rto_target_seconds) or args.rto_target_seconds <= 0:
        parser.error('rto target must be a positive finite number')
    with tempfile.TemporaryDirectory(prefix='bank-c8-drill-') as directory:
        drill = RecoveryDrill(directory, args.rto_target_seconds)
        try:
            report = drill.run()
        except Exception as error:
            # Never echo request bodies, tokens, OTPs, database payloads or child logs.
            report = {'status': 'FAIL', 'scope': 'demo_only_not_production_dr_acceptance',
                      'failed_check': str(error) if isinstance(error, DrillFailure) else type(error).__name__,
                      'checks': drill.checks}
        finally:
            drill.close()
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'PASS' else 1


if __name__ == '__main__':
    raise SystemExit(main())
