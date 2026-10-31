import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from operational_readiness import RuntimeConfig, preflight_report  # noqa: E402


class OperationalReadinessTests(unittest.TestCase):
    def test_demo_runtime_is_ready_without_external_side_effects(self):
        report = preflight_report(RuntimeConfig(
            app_env="demo",
            task_store_backend="memory",
            audit_backend="sqlite-hash-chain",
            redis_url="",
            postgres_dsn="",
            kms_key_alias="",
            siem_endpoint="",
            allow_demo_core=True,
        ))
        self.assertEqual(report["status"], "READY_FOR_CONFIGURED_RUNTIME")
        self.assertFalse(report["blockers"])
        self.assertTrue(report["checked_without_side_effects"])

    def test_production_blocks_demo_core_and_insecure_defaults(self):
        report = preflight_report(RuntimeConfig(
            app_env="production",
            task_store_backend="memory",
            audit_backend="postgres-worm",
            redis_url="",
            postgres_dsn="postgresql://agent:change-me@postgres:5432/agent",
            kms_key_alias="banking-agent-prod",
            siem_endpoint="https://siem.example.invalid/ingest",
            allow_demo_core=True,
        ))
        self.assertEqual(report["status"], "BLOCKED")
        names = {item["name"] for item in report["blockers"]}
        self.assertIn("生产环境禁用 Mock 核心", names)
        self.assertIn("生产任务状态存储", names)
        self.assertIn("PostgreSQL 连接配置", names)
        self.assertIn("SIEM 配置", names)

    def test_production_dependency_check_is_explicit(self):
        with patch.dict(os.environ, {
            "APP_ENV": "production",
            "ALLOW_DEMO_CORE": "false",
            "TASK_STORE_BACKEND": "redis",
            "AUDIT_BACKEND": "postgres-worm",
            "REDIS_URL": "rediss://redis.example:6380/0",
            "POSTGRES_DSN": "postgresql://agent:secret@postgres.example:5432/agent",
            "KMS_KEY_ALIAS": "prod-key",
            "SIEM_ENDPOINT": "https://siem.bank.example/ingest",
        }, clear=False):
            report = preflight_report()
        names = {item["name"] for item in report["blockers"]}
        # Optional clients are intentionally checked instead of silently falling back to memory.
        self.assertIn("生产依赖声明", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
