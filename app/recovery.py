"""C8 single-process checkpoints, reconciliation and authorized DAG delay scheduling.

Production replacement: transactional workflow store/CAS, authenticated status API,
core query adapter and supervised distributed scheduler. In-flight effects are queried,
never retried. Previously authorized, not-yet-started birthday nodes may run when due.
"""
from dataclasses import asdict, fields
from business_tasks import BusinessTaskStore
from datetime import datetime, timezone
from functools import wraps
import hashlib
import hmac
import json
from pathlib import Path
import sqlite3
import threading
import time


class RecoveryStore:
    def __init__(self, audit, path):
        self.audit = audit
        self.path = Path(path)
        self.business_tasks = BusinessTaskStore(audit)
        with audit.lock, audit.db:
            audit.db.execute('''CREATE TABLE IF NOT EXISTS task_checkpoints (
                task_id TEXT PRIMARY KEY, status TEXT NOT NULL,
                payload_json TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            audit.db.execute('CREATE TABLE IF NOT EXISTS readiness_probe (value INTEGER)')

    def save(self, task_id, status, payload):
        raw = json.dumps(payload, ensure_ascii=False, default=lambda v: v.isoformat())
        with self.audit.lock, self.audit.db:
            self.audit.db.execute('''INSERT INTO task_checkpoints VALUES (?,?,?,?)
                ON CONFLICT(task_id) DO UPDATE SET status=excluded.status,
                payload_json=excluded.payload_json, updated_at=excluded.updated_at''',
                (task_id, status, raw, datetime.now(timezone.utc).isoformat()))
            self.business_tasks.checkpoint_locked(task_id, status, payload)

    def rows(self):
        with self.audit.lock:
            return [(row[0], row[1], json.loads(row[2])) for row in self.audit.db.execute(
                'SELECT task_id,status,payload_json FROM task_checkpoints').fetchall()]

    def get(self, task_id):
        with self.audit.lock:
            row = self.audit.db.execute(
                'SELECT status,payload_json FROM task_checkpoints WHERE task_id=?', (task_id,)).fetchone()
        return (row[0], json.loads(row[1])) if row else None

    def writable(self):
        # mode=rw prevents a missing database from being silently re-created.
        # Actual INSERT, always rolled back: no business writes or probe accumulation.
        connection = None
        try:
            connection = sqlite3.connect(self.path.resolve().as_uri() + '?mode=rw', uri=True, timeout=.1)
            connection.execute('BEGIN IMMEDIATE')
            connection.execute('INSERT INTO readiness_probe VALUES (1)')
            return True
        except (sqlite3.Error, OSError):
            return False
        finally:
            if connection is not None:
                connection.rollback()
                connection.close()


class RecoveryScheduler:
    """Only reconcile submitted core transactions; waiting nodes are never executed."""
    def __init__(self, callback, interval=.25, stale_after=5):
        self.callback = callback
        self.interval = interval
        self.stale_after = stale_after
        self._stop = threading.Event()
        self._thread = None
        self._heartbeat = 0.0
        self._failed = False

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._failed = False
        self._heartbeat = 0.0
        self._thread = threading.Thread(target=self._run, name='recovery-reconciler', daemon=True)
        self._thread.start()

    def _run(self):
        try:
            while not self._stop.is_set():
                self.callback()
                self._heartbeat = time.monotonic()
                self._stop.wait(self.interval)
        except Exception:
            # No traceback/payload in logs: readiness exposes only the failure bit.
            self._failed = True

    def running(self):
        return bool(self._thread and self._thread.is_alive() and not self._stop.is_set()
                    and not self._failed and self._heartbeat
                    and time.monotonic() - self._heartbeat < self.stale_after)

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2)


def durable_action(method):
    """Checkpoint before acknowledging; serialize only actions on the same task.

    Different tasks retain the existing core's atomic risk-window checks. Never
    hold a global workflow lock across tools, and never retry a side effect.
    """
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        task_id = (args[0] if args else kwargs.get('plan_id', '')) if method.__name__ != 'chat' else ''
        guard = self.recovery_task_lock(task_id) if task_id else threading.RLock()
        with guard:
            if self.recovery_failed:
                return self.recovery_unavailable()
            plan = self.pending.get(task_id)
            if plan is not None:
                saved = self.recovery_store.get(task_id)
                trusted = saved[1].get('plan', {}) if saved else {}
                if trusted and (plan.plan_id,plan.actor_id,plan.device_id) != (trusted.get('plan_id'),trusted.get('actor_id'),trusted.get('device_id')):
                    credential = self.auth_credentials.latest_for(task_id)
                    if credential is not None:credential.invalidated_reason = 'binding_changed'
                    restored = dict(trusted)
                    restored['steps'] = [self._recovery_step_type(**x) for x in restored['steps']]
                    self.save_task_checkpoint(self._recovery_plan_type(**restored),saved[0])
                    self.audit.write(trusted['trace_id'],'task.binding.rejected',{'task_id':task_id,'reason':'checkpoint_identity_mismatch'},actor=trusted['actor_id'])
                    return {'ok':False,'error_code':'AUTH_BINDING_MISMATCH','message':'任务主体或设备已变化，请重新核对。'}
            try:
                response = method(self, *args, **kwargs)
                if method.__name__ == 'chat':
                    task_id = response.get('plan', {}).get('plan_id', '')
                    plan = self.pending.get(task_id)
                    raw = response.get('plan', {})
                    if plan is None and raw.get('intent') in {'transfer','fraud_guard'}:
                        plan = self._recovery_plan_type(**{f.name:raw[f.name] for f in fields(self._recovery_plan_type) if f.name in raw})
                        state = {'SUCCESS':'succeeded','FAILED':'failed','DEFERRED':'deferred','PROCESSING':'processing','UNKNOWN':'unknown'}.get(response.get('result',{}).get('status'))
                        state = state or ('needs_clarification' if response.get('awaiting_details') else 'blocked')
                        self.save_task_checkpoint(plan,state)
                        plan = None  # A blocked plan must never become pending/executable.
                if plan is not None:
                    if response.get('ok') is False and not response.get('result'):
                        saved = self.recovery_store.get(task_id)
                        trusted = saved[1].get('plan', {}) if saved else {}
                        if trusted and ((plan.plan_id,plan.actor_id,plan.device_id) !=
                                (trusted.get('plan_id'),trusted.get('actor_id'),trusted.get('device_id'))
                                or self._auth_digest(plan) != trusted.get('entity_digest')):
                            # Preserve revocations, but never persist attacker-mutated parameters.
                            restored = dict(trusted)
                            restored['steps'] = [self._recovery_step_type(**x) for x in restored['steps']]
                            self.save_task_checkpoint(self._recovery_plan_type(**restored),saved[0])
                            return response
                    if task_id in self.pending:
                        self.save_task_checkpoint(plan, 'waiting_confirmation')
                        response['task_status'] = 'waiting_confirmation'
                    else:
                        result = response.get('result', {})
                        state = {'SUCCESS': 'succeeded', 'FAILED': 'failed', 'DEFERRED': 'deferred',
                                 'PROCESSING': 'processing', 'UNKNOWN': 'unknown', 'PAUSED': 'paused'}.get(result.get('status'), 'expired')
                        if method.__name__ == 'cancel' and response.get('ok'):
                            state = 'cancelled'
                        self.save_task_checkpoint(plan, state)
                task = self.business_tasks.response(response,method.__name__)
                if task is not None:response['business_task'] = task
                return response
            except sqlite3.Error:
                self.recovery_failed = True
                return self.recovery_unavailable()
    return wrapped


class RecoveryMixin:
    def init_recovery(self, path, plan_type, step_type, credential_type, binding_type):
        self.recovery_lock = threading.RLock()
        self.recovery_failed = False
        self._recovery_task_locks = {}
        self._recovery_plan_type = plan_type
        self._recovery_step_type = step_type
        self.recovery_store = RecoveryStore(self.audit, path)
        self.business_tasks = self.recovery_store.business_tasks
        self.recovery_scheduler = RecoveryScheduler(self.reconcile_recovery_tasks)
        for task_id, state, payload in self.recovery_store.rows():
            with self.audit.lock, self.audit.db:
                self.business_tasks.checkpoint_locked(task_id,state,payload)
            data = dict(payload['plan'])
            data['steps'] = [step_type(**step) for step in data['steps']]
            plan = plan_type(**data)
            if plan.plan_id != task_id:
                raise ValueError('Invalid task checkpoint identity')
            if state == 'waiting_confirmation':
                self.pending[task_id] = plan
            else:
                self.consumed_tokens.add(plan.consent_token)
                self.consumed_confirmations[task_id] = self.recovery_tombstone(plan)
            self.auth_plan_snapshots[task_id] = plan
            recovery_meta = payload.get('recovery_meta', {})
            if isinstance(recovery_meta, dict) and recovery_meta.get('unknown_origin'):
                origins = getattr(self, '_unknown_outcome_origins', {})
                origins[task_id] = recovery_meta['unknown_origin']
                self._unknown_outcome_origins = origins
            for raw in payload.get('credentials', []):
                item = dict(raw)
                item['binding'] = binding_type(**item['binding'])
                for field in ('created_at', 'expires_at', 'used_at'):
                    item[field] = datetime.fromisoformat(item[field]) if item[field] else None
                credential = credential_type(**item)
                self.auth_credentials._items[credential.credential_id] = credential
            if state == 'waiting_confirmation' and plan.requires_strong_auth:
                # Restoring a pending high-risk task must not bypass a C5 audit freeze.
                self.verify_audit_trace(plan.trace_id)
            # Passed/used/expired credentials remain evidence, but no execution authority
            # is restored. confirm always checks binding, expiry and one-time use again.

    @staticmethod
    def recovery_tombstone(plan):
        return {'trace_id': plan.trace_id, 'actor_id': plan.actor_id, 'risk_level': plan.risk_level,
                'intent': plan.intent, 'token_digest': hashlib.sha256(plan.consent_token.encode()).hexdigest()}

    @staticmethod
    def recovery_unavailable():
        return {'ok': False, 'error_code': 'RECOVERY_UNAVAILABLE',
                'message': '任务状态暂时无法确认，请勿重复操作，请联系人工核实。'}

    def save_task_checkpoint(self, plan, status):
        with self.auth_credentials._lock:
            credentials = [asdict(item) for item in self.auth_credentials._items.values()
                           if item.binding.task_id == plan.plan_id]
        payload = {'plan': asdict(plan), 'credentials': credentials}
        if status == 'unknown':
            origin = getattr(self, '_unknown_outcome_origins', {}).get(plan.plan_id)
            if origin:
                payload['recovery_meta'] = {'unknown_origin': origin}
        self.recovery_store.save(plan.plan_id, status, payload)
        if status == 'waiting_confirmation' and self.dag_store is not None:
            self.ensure_plan_dag(plan)

    def recovery_task_lock(self, task_id):
        with self.recovery_lock:
            return self._recovery_task_locks.setdefault(task_id, threading.RLock())

    def reconcile_recovery_tasks(self):
        if self.recovery_failed:
            raise RuntimeError('Checkpoint unavailable')
        for task_id, state, payload in self.recovery_store.rows():
            if state not in {'processing', 'unknown', 'recovery_required'}:
                continue
            guard = self.recovery_task_lock(task_id)
            if guard.acquire(blocking=False):
                try:
                    self._reconcile_recovery_task(task_id)
                finally:
                    guard.release()

        if self.dag_store is not None:
            self.advance_dags()

    def _reconcile_recovery_task(self, task_id):
        state, payload = self.recovery_store.get(task_id)
        plan = payload['plan']
        if state not in {'processing', 'unknown', 'recovery_required', 'succeeded', 'failed', 'reversed'}:
            return
        # Known pre-core failure has no transaction; don't turn it into success.
        key = f'idem:{task_id}:create_transfer'
        if self.dag_store is not None:
            dag = self.dag_store.get('plan-' + task_id)
            if dag:
                key = next((n.idempotency_key for n in dag.nodes if n.tool == 'create_transfer'), key)
        with self.bank.lock:
            row = self.bank._row_for_idempotency(key)
        transaction_id = row['transaction_id'] if row else None
        if (not transaction_id and state not in {'processing', 'recovery_required'}
                and not (plan['intent'] == 'transfer' and state in {'succeeded', 'reversed'})):
            return
        queried = None
        try:
            queried = self.bank.query_transaction(transaction_id) if transaction_id else None
        except (TimeoutError, ConnectionError, OSError):
            queried = None
        final = queried.data.get('status') if queried and queried.ok else None
        # A missing record or failed read proves neither success nor failure.
        # Keep the task unknown and require a later query; never submit again.
        origin = payload.get('recovery_meta', {}).get('unknown_origin') if isinstance(payload.get('recovery_meta', {}), dict) else None
        if final in {'succeeded', 'failed', 'reversed'}:
            new_state = final
        elif final == 'processing' and state == 'unknown' and origin == 'transport_timeout' and self.bank.fault_injection is None:
            # A cleared demo transport timeout now exposes the core's durable
            # processing state; this is distinct from a still-unreachable
            # network, which remains unknown until a terminal result appears.
            new_state = 'processing'
        else:
            new_state = final if final == 'processing' and state != 'unknown' else ('unknown' if state == 'unknown' else 'recovery_required')
        if new_state != state:
            self.audit.write(plan['trace_id'], 'task.recovery.reconciled',
                {'task_id': task_id, 'status': new_state, 'transaction_id': transaction_id,
                 'user_id': plan['actor_id'], 'device_id': 'system',
                 'source': 'core.query_transaction' if queried else 'manual_reconciliation_required'},
                actor='system:recovery', risk_level=plan['risk_level'], intent=plan['intent'])
            self.recovery_store.save(task_id, new_state, payload)

    def task_status(self, task_id, action_token, actor_id, device_id):
        if self.recovery_failed:
            return self.recovery_unavailable()
        with self.recovery_task_lock(task_id):
            entry = self.recovery_store.get(task_id)
            plan = entry[1]['plan'] if entry else {}
            if (not entry or not action_token or not hmac.compare_digest(str(action_token), plan['consent_token'])
                    or actor_id != plan['actor_id'] or device_id != plan['device_id']):
                return {'ok': False, 'error_code': 'TASK_ACCESS_DENIED', 'message': '无法查询该任务。'}
            self._reconcile_recovery_task(task_id)
            if getattr(self, 'dag_store', None) is not None and hasattr(self, 'advance_dags'):
                self.advance_dags()
            state = self.recovery_store.get(task_id)[0]
            message = {'waiting_confirmation': '任务已保存，等待您确认，不会自动执行。',
                       'processing': '银行还在确认这笔交易，请不要重复操作',
                       'unknown': '结果未知，正在核验这笔交易，请不要重复操作',
                       'recovery_required': '结果尚不明确，请勿重复操作，请联系人工核实。',
                       'succeeded': '工具查询结果已确认操作成功。', 'failed': '操作未完成，请查询详情或联系人工核实。',
                       'reversed': '银行已确认交易冲正。'}.get(state, '任务已结束。')
            return {'ok': True, 'task_id': task_id, 'trace_id': plan['trace_id'], 'status': state,
                    'message': message}

    def recovery_readiness(self):
        checks = {'database_writable': self.recovery_store.writable(),
                  'scheduler_running': self.recovery_scheduler.running(),
                  'checkpoint_available': not self.recovery_failed}
        return {'ok': all(checks.values()), 'status': 'READY' if all(checks.values()) else 'NOT_READY',
                'checks': checks, 'scope': 'single_process_demo'}
