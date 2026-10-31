"""可选的 Redis/PostgreSQL 生产后端。

这些实现只在显式配置对应后端并安装客户端依赖后启用；默认演示不会导入或连接
Redis/PostgreSQL。它们负责状态、队列和追加审计，不负责替代银行核心、WORM、SIEM
或 KMS。真实上线仍需完成连接安全、权限、容量、故障演练和审计验收。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import time
from typing import Any, Dict, Optional

from production_contracts import AdapterResult, new_request_id


class BackendDependencyError(RuntimeError):
    pass


def _load(module_name: str):
    try:
        return importlib.import_module(module_name)
    except ImportError as exc:  # pragma: no cover - depends on deployment extras
        raise BackendDependencyError(
            f"未安装生产后端依赖 {module_name}，请安装 requirements-production.txt"
        ) from exc


def _json(value: Dict[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class RedisTaskStore:
    def __init__(self, url: str, prefix: str = "banking-agent:task:", ttl_seconds: int = 86400):
        redis = _load("redis")
        self.client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        self.prefix = prefix
        self.ttl_seconds = ttl_seconds

    def _key(self, task_id: str) -> str:
        return f"{self.prefix}{task_id}"

    def put(self, task_id: str, state: Dict[str, Any]) -> AdapterResult:
        self.client.set(self._key(task_id), _json(state), ex=self.ttl_seconds)
        return AdapterResult(True, "TASK_STORED", "任务状态已写入 Redis", {"task_id": task_id}, state.get("trace_id", ""), new_request_id("redis"))

    def get(self, task_id: str) -> AdapterResult:
        raw = self.client.get(self._key(task_id))
        if raw is None:
            return AdapterResult(False, "TASK_NOT_FOUND", "任务不存在", {}, "", new_request_id("redis"))
        return AdapterResult(True, "TASK_FOUND", "已读取任务状态", json.loads(raw), json.loads(raw).get("trace_id", ""), new_request_id("redis"))

    def update(self, task_id: str, state: Dict[str, Any]) -> AdapterResult:
        return self.put(task_id, state)


class RedisMessageQueue:
    def __init__(self, url: str, prefix: str = "banking-agent:queue:"):
        redis = _load("redis")
        self.client = redis.Redis.from_url(url, decode_responses=True, socket_timeout=2, socket_connect_timeout=2)
        self.prefix = prefix

    def _key(self, topic: str) -> str:
        return f"{self.prefix}{topic}"

    def publish(self, topic: str, payload: Dict[str, Any], message_id: str) -> AdapterResult:
        body = {"message_id": message_id, "published_at": time.time(), "payload": payload}
        self.client.rpush(self._key(topic), _json(body))
        return AdapterResult(True, "MESSAGE_ENQUEUED", "消息已进入 Redis 队列", {"message_id": message_id}, payload.get("trace_id", ""), new_request_id("redis"), message_id)

    def dead_letter(self, topic: str, payload: Dict[str, Any], reason: str) -> AdapterResult:
        body = {"created_at": time.time(), "reason": reason, "payload": payload}
        self.client.rpush(self._key(f"{topic}:dead-letter"), _json(body))
        return AdapterResult(True, "MESSAGE_DEAD_LETTERED", "消息已进入死信队列", {"reason": reason}, payload.get("trace_id", ""), new_request_id("redis"))


class PostgresTaskStore:
    def __init__(self, dsn: str, table: str = "agent_tasks"):
        self.psycopg = _load("psycopg")
        self.dsn = dsn
        self.table = table
        self._ensure_schema()

    def _connect(self):
        return self.psycopg.connect(self.dsn, connect_timeout=3)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE TABLE IF NOT EXISTS {self.table} (task_id TEXT PRIMARY KEY, state_json JSONB NOT NULL, updated_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            conn.commit()

    def put(self, task_id: str, state: Dict[str, Any]) -> AdapterResult:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"INSERT INTO {self.table}(task_id,state_json,updated_at) VALUES (%s,%s::jsonb,now()) ON CONFLICT(task_id) DO UPDATE SET state_json=EXCLUDED.state_json, updated_at=now()", (task_id, _json(state)))
            conn.commit()
        return AdapterResult(True, "TASK_STORED", "任务状态已写入 PostgreSQL", {"task_id": task_id}, state.get("trace_id", ""), new_request_id("postgres"))

    def get(self, task_id: str) -> AdapterResult:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT state_json FROM {self.table} WHERE task_id=%s", (task_id,))
                row = cur.fetchone()
        if not row:
            return AdapterResult(False, "TASK_NOT_FOUND", "任务不存在", {}, "", new_request_id("postgres"))
        state = row[0] if isinstance(row[0], dict) else json.loads(row[0])
        return AdapterResult(True, "TASK_FOUND", "已读取任务状态", state, state.get("trace_id", ""), new_request_id("postgres"))

    def update(self, task_id: str, state: Dict[str, Any]) -> AdapterResult:
        return self.put(task_id, state)


class PostgresAppendOnlyAuditSink:
    """PostgreSQL 追加审计实现；生产仍需在数据库之上接 WORM/对象锁和 SIEM。"""

    def __init__(self, dsn: str, table: str = "agent_audit_events"):
        self.psycopg = _load("psycopg")
        self.dsn = dsn
        self.table = table
        self._ensure_schema()

    def _connect(self):
        return self.psycopg.connect(self.dsn, connect_timeout=3)

    def _ensure_schema(self) -> None:
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"CREATE TABLE IF NOT EXISTS {self.table} (id BIGSERIAL PRIMARY KEY, trace_id TEXT NOT NULL, event_type TEXT NOT NULL, event_json JSONB NOT NULL, prev_hash TEXT, event_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL DEFAULT now())")
            conn.commit()

    def append(self, event: Dict[str, Any]) -> AdapterResult:
        event_json = _json(event)
        with self._connect() as conn:
            with conn.cursor() as cur:
                cur.execute(f"SELECT event_hash FROM {self.table} ORDER BY id DESC LIMIT 1")
                row = cur.fetchone()
                prev_hash = row[0] if row else "GENESIS"
                event_hash = hashlib.sha256((prev_hash + event_json).encode("utf-8")).hexdigest()
                cur.execute(f"INSERT INTO {self.table}(trace_id,event_type,event_json,prev_hash,event_hash) VALUES (%s,%s,%s::jsonb,%s,%s)", (event.get("trace_id", ""), event.get("event_type", "unknown"), event_json, prev_hash, event_hash))
            conn.commit()
        return AdapterResult(True, "AUDIT_APPENDED", "审计事件已追加写入 PostgreSQL", {"event_hash": event_hash}, event.get("trace_id", ""), new_request_id("postgres"))


def build_configured_backends(task_store_backend: str, audit_backend: str, redis_url: str = "", postgres_dsn: str = "") -> Dict[str, Any]:
    """显式构造后端；不会静默回退到内存，避免生产配置看似成功但实际丢状态。"""
    result: Dict[str, Any] = {}
    if task_store_backend == "redis":
        if not redis_url:
            raise ValueError("TASK_STORE_BACKEND=redis 时必须提供 REDIS_URL")
        result["task_store"] = RedisTaskStore(redis_url)
        result["queue"] = RedisMessageQueue(redis_url)
    elif task_store_backend in {"postgres", "postgresql"}:
        if not postgres_dsn:
            raise ValueError("TASK_STORE_BACKEND=postgres 时必须提供 POSTGRES_DSN")
        result["task_store"] = PostgresTaskStore(postgres_dsn)
    elif task_store_backend in {"memory", "sqlite-demo"}:
        result["task_store"] = None
    else:
        raise ValueError(f"不支持的任务状态后端: {task_store_backend}")

    if audit_backend in {"postgres-worm", "siem-worm"}:
        if not postgres_dsn:
            raise ValueError("PostgreSQL 审计后端必须提供 POSTGRES_DSN")
        result["audit"] = PostgresAppendOnlyAuditSink(postgres_dsn)
    elif audit_backend in {"sqlite-hash-chain", "sqlite-hash-chain-demo"}:
        result["audit"] = None
    else:
        raise ValueError(f"不支持的审计后端: {audit_backend}")
    return result
