"""Single-threaded, offline task DAG primitives and migration/storage adapters (D6).

This module deliberately imports no banking runtime and grants no permissions.
The injected execute(node, tool, params, idempotency_key) adapter MUST reuse the
existing registered-tool dispatcher and its last-moment policy/auth checks,
including for compensations. It returns the bank's structured result, not prose.
There are no retries, worker threads, dynamic expressions or model decisions.

on_change(snapshot, event) is a synchronous checkpoint boundary, before dispatch
and after each transition. The integration must persist snapshots privately and
send only event metadata to AuditStore's redacted hash chain. TaskDagStore
connects this boundary to SQLite and the existing AuditStore.
AgentRuntime supplies authenticated dispatch/control; this module grants no authority.
A checkpoint failure stops this executor; recovery must reload durable state.

Results preserve execution and compensation evidence separately. An ambiguous
running action (crash/exception/processing response) waits for reconciliation;
approval of a human_gate cannot certify such an action as successful. Cancellation
means pending work is stopped and declared compensations are attempted, NOT that
irreversible bank transfers are automatically reversed.
"""
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Callable, Dict, List, Optional


NODE_ALLOWED_TRANSITIONS = {
    "pending": frozenset({"ready", "skipped"}),
    "ready": frozenset({"running", "skipped"}),
    "running": frozenset({"succeeded", "failed"}),
    "succeeded": frozenset({"compensated"}),
    "failed": frozenset(), "skipped": frozenset(), "compensated": frozenset(),
}
TASK_ALLOWED_TRANSITIONS = {
    "running": frozenset({"paused", "waiting_human", "failed", "compensating", "completed"}),
    "paused": frozenset({"running", "compensating"}),
    "waiting_human": frozenset({"running", "handoff"}),
    "compensating": frozenset({"cancelled"}),
    "failed": frozenset(), "completed": frozenset(),
    "cancelled": frozenset(), "handoff": frozenset(),
}


def _text(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(label + " must be a non-empty string")


def _params(value: Any, key: str = "") -> None:
    """All monetary values are integer cents; never coerce floats/strings/bools."""
    if key.endswith("_cents") or key in {"amount", "balance", "budget", "price", "fee"}:
        if type(value) is not int:
            raise ValueError("monetary parameters must be integer cents")
    if isinstance(value, dict):
        for name, item in value.items():
            if not isinstance(name, str):
                raise ValueError("parameter keys must be strings")
            _params(item, name)
    elif isinstance(value, list):
        for item in value:
            _params(item)
    elif value is not None and type(value) not in (str, int, bool):
        raise ValueError("parameters must be JSON values without floats")


def _instant(value: str) -> datetime:
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("delay run_at must be an ISO timestamp") from exc
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("delay run_at must include timezone")
    return result


@dataclass
class DagNode:
    node_id: str
    node_type: str = "action"
    tool: str = ""
    params: Dict[str, Any] = field(default_factory=dict)
    depends_on: List[str] = field(default_factory=list)
    compensation: Optional[Dict[str, Any]] = None
    status: str = "pending"
    result: Dict[str, Any] = field(default_factory=dict)
    idempotency_key: str = ""

    def __post_init__(self) -> None:
        _text(self.node_id, "node_id")
        if ":" in self.node_id:
            raise ValueError("node_id must not contain ':'")
        if self.node_type not in {"action", "delay", "human_gate"}:
            raise ValueError("unknown node type")
        if self.status not in NODE_ALLOWED_TRANSITIONS:
            raise ValueError("unknown node status")
        if not isinstance(self.params, dict) or not isinstance(self.result, dict):
            raise ValueError("params and result must be dictionaries")
        _params(self.params)
        if not isinstance(self.depends_on, list) or any(not isinstance(d, str) or not d for d in self.depends_on):
            raise ValueError("depends_on must be a list of node ids")
        if self.node_type == "action":
            _text(self.tool, "action tool")
        elif self.tool or self.compensation is not None:
            raise ValueError("only action nodes may dispatch tools/compensations")
        if self.node_type == "delay":
            _instant(self.params.get("run_at"))
        if self.compensation is not None:
            if not isinstance(self.compensation, dict) or set(self.compensation) != {"tool", "params"}:
                raise ValueError("compensation requires only tool and params")
            _text(self.compensation["tool"], "compensation tool")
            if not isinstance(self.compensation["params"], dict):
                raise ValueError("compensation params must be a dictionary")
            _params(self.compensation["params"])
        # Reject snapshots that cannot be durably represented as strict JSON.
        json.dumps(self.result, allow_nan=False)

    def transition(self, target: str, dependencies: Optional[Dict[str, str]] = None) -> None:
        if target not in NODE_ALLOWED_TRANSITIONS[self.status]:
            raise ValueError("illegal node transition: %s -> %s" % (self.status, target))
        if target == "ready" and any((dependencies or {}).get(d) != "succeeded" for d in self.depends_on):
            raise ValueError("all dependencies must have succeeded")
        self.status = target


@dataclass
class TaskDag:
    dag_id: str
    plan_id: str
    intent: str
    trace_id: str
    actor_id: str
    nodes: List[DagNode]
    status: str = "running"
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("dag_id", "plan_id", "intent", "trace_id", "actor_id"):
            _text(getattr(self, name), name)
        if not isinstance(self.metadata, dict):
            raise ValueError("DAG metadata must be a dictionary")
        json.dumps(self.metadata, allow_nan=False)
        if ":" in self.dag_id:
            raise ValueError("dag_id must not contain ':'")
        if self.status not in TASK_ALLOWED_TRANSITIONS:
            raise ValueError("unknown task status")
        if not isinstance(self.nodes, list) or not self.nodes or any(not isinstance(n, DagNode) for n in self.nodes):
            raise ValueError("DAG requires non-empty DagNode list")
        self.topological_order()  # Reject invalid graphs before any execution.
        for node in self.nodes:
            expected = "idem:%s:%s" % (self.dag_id, node.node_id)
            if node.idempotency_key and node.idempotency_key != expected:
                raise ValueError("node idempotency key does not match DAG identity")
            node.idempotency_key = expected

    def topological_order(self) -> List[DagNode]:
        """Stable Kahn sort; includes disconnected components in cycle checking."""
        by_id = {node.node_id: node for node in self.nodes}
        if len(by_id) != len(self.nodes):
            raise ValueError("duplicate node id")
        remaining = {}
        followers = {name: [] for name in by_id}
        for node in self.nodes:
            if len(node.depends_on) != len(set(node.depends_on)):
                raise ValueError("duplicate dependency")
            remaining[node.node_id] = len(node.depends_on)
            for parent in node.depends_on:
                if parent not in by_id:
                    raise ValueError("unknown dependency")
                followers[parent].append(node.node_id)
        queue = [name for name in by_id if remaining[name] == 0]
        ordered = []
        for name in queue:
            ordered.append(by_id[name])
            for child in followers[name]:
                remaining[child] -= 1
                if remaining[child] == 0:
                    queue.append(child)
        if len(ordered) != len(self.nodes):
            raise ValueError("cycle detected in task DAG")
        return ordered

    def transition(self, target: str) -> None:
        if target not in TASK_ALLOWED_TRANSITIONS[self.status]:
            raise ValueError("illegal task transition: %s -> %s" % (self.status, target))
        self.status = target

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, state: Dict[str, Any]) -> "TaskDag":
        state = deepcopy(state)
        state["nodes"] = [DagNode(**node) for node in state["nodes"]]
        return cls(**state)


class DagExecutor:
    """Cooperative single-thread scheduler; call run again when a delay is due.

    pause takes effect between tools, never interrupts a dispatched bank call.
    The host MUST authenticate/authorize all controls, including approve/handoff.
    This is not a standalone public authorization or credential API.
    """

    def __init__(self, dag: TaskDag, execute: Callable,
                 on_change: Optional[Callable] = None, clock: Optional[Callable] = None):
        self.dag = dag
        self._execute = execute
        self._on_change = on_change or (lambda snapshot, event: None)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._in_run = False
        self._broken = False
        self._dispatching = False

    def _healthy(self) -> None:
        if self._broken:
            raise RuntimeError("checkpoint failed; reload durable state before proceeding")

    def _checkpoint(self, event_type: str, node: Optional[DagNode] = None, **data: Any) -> None:
        event = {"event_type": event_type, "dag_id": self.dag.dag_id,
                 "plan_id": self.dag.plan_id, "trace_id": self.dag.trace_id,
                 "actor_id": self.dag.actor_id, **data}
        if node is not None:
            event.update(node_id=node.node_id, idempotency_key=node.idempotency_key)
        try:
            self._on_change(self.dag.to_dict(), deepcopy(event))
        except Exception:
            self._broken = True
            raise

    def _node_transition(self, node: DagNode, target: str) -> None:
        previous = node.status
        node.transition(target, {n.node_id: n.status for n in self.dag.nodes})
        self._checkpoint("dag.node.transition", node, **{"from": previous, "to": target})

    def _task_transition(self, target: str) -> None:
        previous = self.dag.status
        self.dag.transition(target)
        self._checkpoint("dag.transition", **{"from": previous, "to": target})

    def _call(self, node: DagNode, tool: str, params: Dict[str, Any], key: str) -> Dict[str, Any]:
        self._dispatching = True
        try:
            # Snapshots prevent an adapter mutating the plan/result outside checkpoints.
            result = self._execute(deepcopy(node), tool, deepcopy(params), key)
            if not isinstance(result, dict) or type(result.get("ok")) is not bool:
                return {"ok": False, "outcome_unknown": True, "error_code": "INVALID_TOOL_RESULT"}
            json.dumps(result, allow_nan=False)
            return deepcopy(result)
        except Exception:
            if self._broken:
                raise  # A callback's failed durable checkpoint must not be swallowed.
            # Never log exception text: it may contain raw bank credentials or PII.
            # A timeout/exception cannot prove that the bank did not execute.
            return {"ok": False, "outcome_unknown": True, "error_code": "TOOL_OUTCOME_UNKNOWN"}
        finally:
            self._dispatching = False

    @staticmethod
    def _outcome(result: Dict[str, Any]) -> str:
        data = result.get("data")
        statuses = []
        for container in (result, data):
            if isinstance(container, dict) and "status" in container:
                status = container["status"]
                if not isinstance(status, str):
                    return "unknown"
                statuses.append(status.lower())
        if result.get("outcome_unknown") or any(s in {"created", "processing", "pending"} for s in statuses):
            return "unknown"
        if any(s not in {"succeeded", "success", "failed", "reversed"} for s in statuses):
            return "unknown"
        if not result["ok"] or any(s in {"failed", "reversed"} for s in statuses):
            return "failed"
        return "succeeded"

    def _wait_for_reconciliation(self, node: DagNode) -> None:
        self._checkpoint("dag.reconciliation.required", node)
        if self.dag.status == "paused":
            # Preserve an explicit pause; resume + run will encounter the same barrier.
            return
        self._task_transition("waiting_human")

    def run(self) -> TaskDag:
        self._healthy()
        if self._in_run:
            raise RuntimeError("DAG executor is not reentrant")
        if self.dag.status != "running":
            return self.dag
        self._in_run = True
        try:
            ordered = self.dag.topological_order()
            for node in ordered:
                if node.status == "running":
                    if node.node_type == "human_gate":
                        self._task_transition("waiting_human")
                    else:
                        self._wait_for_reconciliation(node)
                    return self.dag
            # A crash after recording failure but before compensating must not
            # allow an independent branch to execute during recovery.
            if any(n.status == "failed" for n in ordered):
                self._fail()
                return self.dag
            for node in ordered:
                if self.dag.status != "running":
                    break
                if node.status == "pending" and all(
                    next(n for n in ordered if n.node_id == parent).status == "succeeded"
                    for parent in node.depends_on
                ):
                    self._node_transition(node, "ready")
                if node.status != "ready":
                    continue
                if any(next(n for n in ordered if n.node_id == parent).status != "succeeded"
                       for parent in node.depends_on):
                    raise ValueError("ready node has unsatisfied dependencies")
                if node.node_type == "delay" and self._clock() < _instant(node.params["run_at"]):
                    continue
                self._node_transition(node, "running")
                if node.node_type == "human_gate":
                    self._task_transition("waiting_human")
                    break
                if node.node_type == "delay":
                    result = {"ok": True, "data": {"run_at": node.params["run_at"]}}
                else:
                    result = self._call(node, node.tool, node.params, node.idempotency_key)
                node.result["execution"] = result
                outcome = self._outcome(result)
                if outcome == "unknown":
                    self._wait_for_reconciliation(node)
                    break
                self._node_transition(node, outcome)
                if outcome == "failed":
                    self._fail()
                    break
            if self.dag.status == "running" and all(n.status == "succeeded" for n in ordered):
                self._task_transition("completed")
            return self.dag
        finally:
            self._in_run = False

    def _skip_pending(self) -> None:
        for node in self.dag.topological_order():
            if node.status in {"pending", "ready"}:
                self._node_transition(node, "skipped")

    def _compensate(self) -> bool:
        for node in reversed(self.dag.topological_order()):
            if node.status != "succeeded" or node.compensation is None:
                continue
            previous = node.result.get("compensation")
            if previous is not None:
                # Including crash-in-flight: NEVER automatically retry compensation.
                return False
            node.result["compensation"] = {"status": "running"}
            self._checkpoint("dag.compensation.started", node)
            result = self._call(node, node.compensation["tool"], node.compensation["params"],
                                node.idempotency_key + ":compensate")
            outcome = self._outcome(result)
            node.result["compensation"] = {"status": outcome, "result": result}
            if outcome == "succeeded":
                self._node_transition(node, "compensated")
            else:
                self._checkpoint("dag.compensation." + outcome, node)
                # Do not release an upstream resource while its dependent undo
                # has failed or remains uncertain. Require manual reconciliation.
                return False
        return True

    def _fail(self) -> None:
        # The requested task state machine forbids failed -> compensating.
        # Therefore fail-fast stops downstream work, compensates while running,
        # then records failed. 'compensating -> cancelled' is for explicit cancel.
        if self.dag.status == "paused":
            self._task_transition("running")
        self._skip_pending()
        self._compensate()
        self._task_transition("failed")

    def pause(self) -> None:
        self._healthy()
        self._task_transition("paused")

    def resume(self) -> None:
        self._healthy()
        if self.dag.status != "paused":
            raise ValueError("only paused DAGs may resume; human gates require approval")
        self._task_transition("running")

    def cancel(self) -> None:
        self._healthy()
        if self._dispatching or any(n.status == "running" for n in self.dag.nodes):
            raise ValueError("cannot cancel an unresolved running node")
        self._task_transition("compensating")
        self._skip_pending()
        if self._compensate():
            self._task_transition("cancelled")

    def approve(self, node_id: str, approved: bool = True) -> None:
        self._healthy()
        node = next((n for n in self.dag.nodes if n.node_id == node_id), None)
        if (self.dag.status != "waiting_human" or node is None
                or node.node_type != "human_gate" or node.status != "running"
                or type(approved) is not bool):
            raise ValueError("only an awaiting human gate can receive an explicit decision")
        node.result["execution"] = {"ok": approved, "data": {"approved": approved}}
        self._node_transition(node, "succeeded" if approved else "failed")
        self._task_transition("running")
        if not approved:
            self._fail()

    def reconcile(self, node_id: str, result: Dict[str, Any]) -> None:
        """Host supplies a trusted read-only core query, never a user's claimed result."""
        self._healthy()
        node = next(n for n in self.dag.nodes if n.node_id == node_id)
        if node.status != 'running' or node.node_type != 'action':
            raise ValueError('only an in-flight action may be reconciled')
        outcome = self._outcome(result)
        if outcome == 'unknown':
            return
        node.result['execution'] = deepcopy(result)
        self._node_transition(node, outcome)
        if self.dag.status == 'waiting_human':
            self._task_transition('running')
        if outcome == 'failed' and self.dag.status == 'running':
            self._fail()

    def handoff(self) -> None:
        self._healthy()
        self._task_transition("handoff")


# ----------------------- conversion and durable snapshots --------------------
def plan_to_dag(plan: Any) -> TaskDag:
    """Convert a trusted AgentPlan to a chain; conversion never grants consent.

    No credentials are copied. Stage 3 must recheck the live PolicyEngine and
    credential store when dispatching. Completed ToolResult data comes only from
    the old step detail, not from the step title or generated assistant prose.
    """
    source = asdict(plan) if not isinstance(plan, dict) else deepcopy(plan)
    nodes = []
    previous = []
    if source.get('requires_confirmation') or source.get('requires_strong_auth'):
        gate = DagNode('confirmation', node_type='human_gate', params={
            'requires_confirmation': bool(source.get('requires_confirmation')),
            'requires_strong_auth': bool(source.get('requires_strong_auth'))})
        nodes.append(gate)
        previous = [gate.node_id]
    states = {'pending': 'pending', 'running': 'running', 'completed': 'succeeded',
              'failed': 'failed', 'skipped': 'skipped', 'deferred': 'running'}
    for step in source['steps']:
        if step['status'] not in states:
            raise ValueError('unknown legacy plan step status')
        status = states[step['status']]
        result = {}
        if step['status'] == 'completed':
            try:
                data = json.loads(step['detail'])
            except (KeyError, TypeError, ValueError) as exc:
                raise ValueError('completed step needs structured tool evidence') from exc
            if not isinstance(data, dict):
                raise ValueError('completed step tool evidence must be a dictionary')
            execution = {'ok': True, 'tool': step['tool'], 'data': data}
            outcome = DagExecutor._outcome(execution)
            # Domain statuses (e.g. LOCKED) are not bank transaction statuses.
            # For transfers specifically, processing can never mean succeeded.
            if step['tool'] == 'create_transfer':
                status = 'running' if outcome == 'unknown' else outcome
            result['execution'] = execution
        elif step['status'] == 'failed':
            result['execution'] = {'ok': False, 'error_message': step.get('detail', '')}
        node = DagNode(step['id'], tool=step['tool'], params={k: deepcopy(v) for k, v in source['entities'].items() if v is not None},
                       depends_on=previous, status=status, result=result)
        nodes.append(node)
        previous = [node.node_id]
    return TaskDag('plan-' + source['plan_id'], source['plan_id'], source['intent'],
                   source['trace_id'], source['actor_id'], nodes,
                   metadata={'source': 'AgentPlan', 'execution_owner': 'legacy',
                             'device_id': source.get('device_id', '')})


def linkage_to_dag(task: Dict[str, Any]) -> TaskDag:
    """Pure, lossless migration of the existing A/B/C birthday representation.

    Delays B/C depend on the budget lock independently: the legacy schedule was
    date-driven, not C-depends-on-B. Historical timestamps, order IDs, errors and
    compensation evidence are retained in a private legacy_state snapshot. The
    converter defaults to legacy ownership; AgentRuntime explicitly adopts it.
    """
    task = deepcopy(task)
    for name in ('task_id', 'trace_id', 'actor_id', 'birthday_date', 'created_at'):
        _text(task.get(name), name)
    amount = task.get('lock_amount_cents')
    if type(amount) is not int or amount <= 0:
        raise ValueError('birthday lock amount must be positive integer cents')
    task_states = {'ACTIVE': 'running', 'COMPLETED': 'completed', 'CANCELLED': 'cancelled', 'FAILED': 'failed'}
    node_states = {'PENDING': 'pending', 'COMPLETED': 'succeeded', 'FAILED': 'failed',
                   'CANCELLED': 'skipped', 'COMPENSATED': 'compensated'}
    if task.get('status') not in task_states:
        raise ValueError('unknown legacy task status')
    raw_nodes = task.get('nodes')
    if not isinstance(raw_nodes, list) or len(raw_nodes) != 3 or any(not isinstance(n, dict) for n in raw_nodes):
        raise ValueError('legacy birthday task requires exactly A/B/C')
    by_id = {n.get('node_id'): n for n in raw_nodes}
    if set(by_id) != {'A', 'B', 'C'}:
        raise ValueError('legacy birthday nodes must be unique A/B/C')
    expected_tools = {'A': 'lock_subaccount', 'B': 'order_flowers', 'C': 'order_cake'}
    nodes = []
    for name in 'ABC':
        old = by_id[name]
        if old.get('action') != expected_tools[name] or old.get('status') not in node_states:
            raise ValueError('unknown legacy node action/status')
        _instant(old.get('due_at'))
        status = node_states[old['status']]
        if name != 'A':
            if status == 'compensated':
                raise ValueError('legacy gift orders have no declared compensation')
            delay_status = ('succeeded' if status in {'succeeded', 'failed'}
                            else 'skipped' if status == 'skipped' else 'pending')
            nodes.append(DagNode('delay-' + name, node_type='delay',
                                 params={'run_at': old['due_at']}, depends_on=['A'], status=delay_status))
        params = {'task_id': task['task_id'], 'actor_id': task['actor_id']}
        compensation = None
        if name == 'A':
            params['amount_cents'] = amount
            compensation = {'tool': 'compensate_subaccount_lock', 'params': {'task_id': task['task_id']}}
        evidence = {}
        if status in {'succeeded', 'compensated'}:
            if not isinstance(old.get('result'), dict):
                raise ValueError('executed legacy node has no structured tool evidence')
            evidence['execution'] = {'ok': True, 'tool': old['action'], 'data': old['result']}
        if status == 'compensated':
            if not isinstance(old.get('compensation'), dict):
                raise ValueError('compensated legacy lock has no tool evidence')
            evidence['compensation'] = {'status': 'succeeded', 'result': {'ok': True, 'data': old['compensation']}}
        if status == 'failed':
            evidence['execution'] = {'ok': False, 'error_message': old.get('error', '')}
        nodes.append(DagNode(name, tool=old['action'], params=params,
                             depends_on=[] if name == 'A' else ['delay-' + name],
                             compensation=compensation, status=status, result=evidence))
    return TaskDag('linkage-' + task['task_id'], task.get('plan_id') or task['task_id'],
                   'birthday_linkage', task['trace_id'], task['actor_id'], nodes,
                   status=task_states[task['status']], metadata={
                       'source': 'linkage_tasks', 'execution_owner': 'legacy', 'legacy_state': task})


class TaskDagStore:
    """Use AuditStore's SQLite connection/lock for atomic snapshot + chain events.

    State JSON is private storage, not an audit payload. AuditStore sanitizes all
    emitted metadata. No bank methods are called by storage or migration.
    """

    def __init__(self, audit: Any):
        self.audit = audit
        with audit.lock:
            audit.db.execute('''CREATE TABLE IF NOT EXISTS task_dags (
                dag_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, actor_id TEXT NOT NULL,
                status TEXT NOT NULL, state_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL)''')
            audit.db.commit()

    def get(self, dag_id: str) -> Optional[TaskDag]:
        with self.audit.lock:
            row = self.audit.db.execute('SELECT state_json FROM task_dags WHERE dag_id=?', (dag_id,)).fetchone()
        return TaskDag.from_dict(json.loads(row[0])) if row else None

    def list_all(self) -> List[TaskDag]:
        with self.audit.lock:
            rows = self.audit.db.execute('SELECT state_json FROM task_dags ORDER BY created_at,dag_id').fetchall()
        return [TaskDag.from_dict(json.loads(row[0])) for row in rows]

    def list_recoverable(self) -> List[TaskDag]:
        with self.audit.lock:
            rows = self.audit.db.execute(
                "SELECT state_json FROM task_dags WHERE status IN ('running','paused','waiting_human','compensating') ORDER BY created_at,dag_id"
            ).fetchall()
        return [TaskDag.from_dict(json.loads(row[0])) for row in rows]

    def _write(self, dag: TaskDag, events: List[Dict[str, Any]], insert_only: bool = False,
               commit: bool = True) -> bool:
        # Validate before SQL; never coerce IDs, monetary parameters or graph states.
        dag = TaskDag.from_dict(dag.to_dict())
        audit = self.audit
        with audit.lock:
            try:
                existing = self.get(dag.dag_id)
                if existing:
                    if (existing.trace_id, existing.actor_id, existing.plan_id, existing.intent) != (
                            dag.trace_id, dag.actor_id, dag.plan_id, dag.intent):
                        raise ValueError('DAG identity is immutable')
                    if insert_only:
                        return False
                timestamp = datetime.now(timezone.utc).isoformat()
                audit.db.execute('''INSERT INTO task_dags
                    (dag_id,trace_id,actor_id,status,state_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(dag_id) DO UPDATE SET status=excluded.status,
                    state_json=excluded.state_json, updated_at=excluded.updated_at''',
                    (dag.dag_id, dag.trace_id, dag.actor_id, dag.status,
                     json.dumps(dag.to_dict(), ensure_ascii=False, allow_nan=False), timestamp, timestamp))
                for event in events:
                    payload = dict(event, task_id=dag.dag_id, dag_id=dag.dag_id,
                                   plan_id=dag.plan_id, actor_id=dag.actor_id)
                    audit.write(dag.trace_id, event['event_type'], payload,
                                actor=dag.actor_id, intent=dag.intent, commit=False)
                if commit:
                    audit.db.commit()
                return True
            except Exception:
                audit.db.rollback()
                raise

    def save(self, dag: TaskDag, event: Dict[str, Any], insert_only: bool = False) -> bool:
        return self._write(dag, [event], insert_only=insert_only)

    def checkpoint(self, snapshot: Dict[str, Any], event: Dict[str, Any]) -> None:
        self.save(TaskDag.from_dict(snapshot), event)

    def sync_linkage(self, task: Dict[str, Any], commit: bool = True) -> bool:
        """Update only legacy-owned mirrors, never overwrite an adopted DAG.

        Migration itself remains insert-only. This explicit bridge keeps the
        old path authoritative through flag rollbacks until stage 3 removes it.
        Imported transitions are evidence, not replayed executor transitions.
        """
        dag = linkage_to_dag(task)
        with self.audit.lock:
            existing = self.get(dag.dag_id)
            if existing and existing.metadata.get('execution_owner') != 'legacy':
                raise ValueError('legacy scheduler may not overwrite an adopted DAG')
            if existing and existing.metadata.get('legacy_state') == task:
                return False
            events = [{'event_type': 'dag.legacy.synced', 'execution_owner': 'legacy'}]
            if existing:
                old = {n.node_id: n for n in existing.nodes}
                for node in dag.nodes:
                    if node.node_id not in old or old[node.node_id].status != node.status:
                        events.append({'event_type': 'dag.node.imported', 'node_id': node.node_id,
                                       'from': old[node.node_id].status if node.node_id in old else None,
                                       'to': node.status, 'idempotency_key': node.idempotency_key})
                if existing.status != dag.status:
                    events.append({'event_type': 'dag.state.imported', 'from': existing.status, 'to': dag.status})
            return self._write(dag, events, commit=commit)


def migrate_linkage_to_dag(audit: Any, store: TaskDagStore) -> List[str]:
    """Repeatable startup import: legacy rows untouched; existing DAGs untouched."""
    with audit.lock:
        # Validate the entire pending import before writing any of its records.
        converted = [linkage_to_dag(task) for task in audit.list_linkage_tasks()
                     if store.get('linkage-' + task['task_id']) is None]
        result = []
        try:
            for dag in converted:
                if store._write(dag, [{'event_type': 'dag.migrated', 'source': 'linkage_tasks',
                                       'execution_owner': 'legacy'}], insert_only=True, commit=False):
                    result.append(dag.dag_id)
            audit.db.commit()
        except Exception:
            audit.db.rollback()
            raise
        return result


def linkage_view(dag: TaskDag) -> Dict[str, Any]:
    """Read-only compatibility view; never writes the legacy table."""
    task = deepcopy(dag.metadata['legacy_state'])
    states = {'pending': 'PENDING', 'ready': 'PENDING', 'running': 'PENDING',
              'succeeded': 'COMPLETED', 'failed': 'FAILED', 'skipped': 'CANCELLED',
              'compensated': 'COMPENSATED'}
    by_id = {n.node_id: n for n in dag.nodes}
    for old in task['nodes']:
        node = by_id[old['node_id']]
        old['status'] = states[node.status]
        execution = node.result.get('execution', {})
        result = execution.get('tool_result', execution)
        if result.get('ok'):
            old['result'] = deepcopy(result.get('data', {}))
        if node.status == 'failed':
            old['error'] = result.get('error_message', '工具执行失败')
        comp = node.result.get('compensation', {}).get('result', {})
        if comp:
            old['compensation'] = deepcopy(comp.get('tool_result', comp).get('data', {}))
    task['status'] = {'completed': 'COMPLETED', 'cancelled': 'CANCELLED',
                      'failed': 'FAILED', 'handoff': 'HANDOFF'}.get(dag.status, 'ACTIVE')
    return task
