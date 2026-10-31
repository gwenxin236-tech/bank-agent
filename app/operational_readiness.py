"""运行时配置与生产前置检查。

这个模块不尝试替代银行侧联调、测评或监管审批；它只把“能否启动、配置是否安全、
生产依赖是否声明”变成可重复执行的机器检查，避免把 Demo 配置误带入生产。
"""
from __future__ import annotations

import importlib.util
import os
import socket
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse
from policy_config import load_policy, PolicyConfigError


_DEMO_VALUES = {
    "change-me",
    "change-me-in-production",
    "local-demo-only",
    "demo",
    "demo-secret",
    "123456",
    "postgresql://agent:change-me@postgres:5432/agent",
}


@dataclass(frozen=True)
class RuntimeConfig:
    app_env: str
    task_store_backend: str
    audit_backend: str
    redis_url: str
    postgres_dsn: str
    kms_key_alias: str
    siem_endpoint: str
    allow_demo_core: bool

    @classmethod
    def from_env(cls) -> "RuntimeConfig":
        return cls(
            app_env=os.getenv("APP_ENV", "demo").strip().lower(),
            task_store_backend=os.getenv("TASK_STORE_BACKEND", "memory").strip().lower(),
            audit_backend=os.getenv("AUDIT_BACKEND", "sqlite-hash-chain").strip().lower(),
            redis_url=os.getenv("REDIS_URL", "").strip(),
            postgres_dsn=os.getenv("POSTGRES_DSN", "").strip(),
            kms_key_alias=os.getenv("KMS_KEY_ALIAS", "").strip(),
            siem_endpoint=os.getenv("SIEM_ENDPOINT", "").strip(),
            allow_demo_core=os.getenv("ALLOW_DEMO_CORE", "true").strip().lower() in {"1", "true", "yes"},
        )

    @property
    def production(self) -> bool:
        return self.app_env in {"prod", "production"}


def _is_demo_value(value: str) -> bool:
    value = value.strip()
    if not value:
        return True
    if value in _DEMO_VALUES:
        return True
    lowered = value.lower()
    return any(token in lowered for token in ("example.invalid", "change-me", "local-demo", "demo-only"))


def _dsn_host(value: str) -> Optional[str]:
    if not value:
        return None
    parsed = urlparse(value)
    return parsed.hostname


def _dependency_status(module_name: str) -> Dict[str, Any]:
    available = importlib.util.find_spec(module_name) is not None
    return {"module": module_name, "available": available}


def _tcp_probe(host: Optional[str], port: int, timeout: float = 0.15) -> Dict[str, Any]:
    """只做短连接探测，不发送业务数据，也不把探测失败当成交易失败。"""
    if not host:
        return {"configured": False, "reachable": False, "reason": "未配置主机"}
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return {"configured": True, "reachable": True, "host": host, "port": port}
    except OSError as exc:
        return {"configured": True, "reachable": False, "host": host, "port": port, "reason": exc.__class__.__name__}


def preflight_report(config: Optional[RuntimeConfig] = None, probe_network: bool = False, policy_path=None) -> Dict[str, Any]:
    cfg = config or RuntimeConfig.from_env()
    checks: List[Dict[str, Any]] = []

    def check(name: str, ok: bool, severity: str, evidence: str, action: str = "") -> None:
        checks.append({"name": name, "ok": bool(ok), "severity": severity, "evidence": evidence, "action": action})

    try:
        policy = load_policy(policy_path)
        check("策略配置完整性", True, "blocker", "已校验版本：" + policy["version"])
    except PolicyConfigError as exc:
        check("策略配置完整性", False, "blocker", str(exc), "修复 policy.json 的必填字段、类型及上下界后重启")

    check(
        "运行环境明确",
        cfg.app_env in {"demo", "test", "staging", "prod", "production"},
        "blocker",
        cfg.app_env or "未设置",
        "APP_ENV 必须是 demo、test、staging 或 production",
    )
    check(
        "生产环境禁用 Mock 核心",
        not cfg.production or not cfg.allow_demo_core,
        "blocker",
        f"APP_ENV={cfg.app_env}, ALLOW_DEMO_CORE={cfg.allow_demo_core}",
        "生产必须设置 ALLOW_DEMO_CORE=false 并接入真实核心账务",
    )
    check(
        "生产任务状态存储",
        not cfg.production or cfg.task_store_backend in {"redis", "postgres", "postgresql"},
        "blocker",
        cfg.task_store_backend,
        "生产使用 Redis 或 PostgreSQL 集群，不得使用内存状态",
    )
    check(
        "生产审计后端",
        not cfg.production or cfg.audit_backend in {"worm", "postgres-worm", "siem-worm"},
        "blocker",
        cfg.audit_backend,
        "生产使用 WORM/对象锁审计并接入 SIEM",
    )
    check(
        "Redis 连接配置",
        not cfg.production or (cfg.task_store_backend != "redis" or bool(cfg.redis_url)),
        "blocker",
        "已配置" if cfg.redis_url else "未配置",
        "设置 REDIS_URL 并完成集群、TLS 和故障演练",
    )
    check(
        "PostgreSQL 连接配置",
        not cfg.production or (cfg.task_store_backend not in {"postgres", "postgresql"} and cfg.audit_backend not in {"postgres-worm", "siem-worm"} or (bool(cfg.postgres_dsn) and not _is_demo_value(cfg.postgres_dsn))),
        "blocker",
        "已配置" if cfg.postgres_dsn else "未配置",
        "设置 POSTGRES_DSN 并完成高可用、备份恢复与权限隔离",
    )
    check(
        "密钥管理配置",
        not cfg.production or (bool(cfg.kms_key_alias) and not _is_demo_value(cfg.kms_key_alias)),
        "blocker",
        cfg.kms_key_alias or "未配置",
        "配置 KMS/Vault/HSM 的非演示密钥别名",
    )
    check(
        "SIEM 配置",
        not cfg.production or (bool(cfg.siem_endpoint) and not _is_demo_value(cfg.siem_endpoint)),
        "blocker",
        cfg.siem_endpoint or "未配置",
        "配置受信任的 SIEM 入口和服务身份",
    )
    check(
        "生产依赖声明",
        not cfg.production or (_dependency_status("redis")["available"] and _dependency_status("psycopg")["available"]),
        "blocker",
        f"redis={_dependency_status('redis')['available']}, psycopg={_dependency_status('psycopg')['available']}",
        "安装并锁定 redis 与 psycopg 生产客户端依赖",
    )

    if probe_network and cfg.production:
        redis_host = _dsn_host(cfg.redis_url)
        postgres_host = _dsn_host(cfg.postgres_dsn)
        checks.append({"name": "Redis 网络探测", "ok": _tcp_probe(redis_host, 6379)["reachable"], "severity": "warning", "evidence": _tcp_probe(redis_host, 6379), "action": "确认生产网络策略、TLS 和服务发现"})
        checks.append({"name": "PostgreSQL 网络探测", "ok": _tcp_probe(postgres_host, 5432)["reachable"], "severity": "warning", "evidence": _tcp_probe(postgres_host, 5432), "action": "确认生产网络策略、TLS 和服务发现"})

    blockers = [item for item in checks if item["severity"] == "blocker" and not item["ok"]]
    warnings = [item for item in checks if item["severity"] == "warning" and not item["ok"]]
    return {
        "status": "READY_FOR_CONFIGURED_RUNTIME" if not blockers else "BLOCKED",
        "app_env": cfg.app_env,
        "production_mode": cfg.production,
        "checked_without_side_effects": True,
        "checks": checks,
        "blockers": blockers,
        "warnings": warnings,
        "next_actions": [item["action"] for item in blockers if item.get("action")],
    }
