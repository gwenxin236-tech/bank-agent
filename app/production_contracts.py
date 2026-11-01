"""生产化依赖契约和本地 Mock 实现。

这些接口是进入真实生产环境前的替换边界：Agent 只能通过契约访问身份、设备、强认证、核心账务、支付、风控、反洗钱、通知、任务状态、消息队列、审计、密钥和字段加密服务。
当前竞赛环境只提供安全的 Mock/Noop 实现，不代表已经完成真实银行系统联调或取得商用资质。
"""
from __future__ import annotations

from policy_config import get_policy

import base64
import hashlib
import hmac
import os
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Protocol, Tuple


def new_request_id(prefix: str = "req") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:16]}"


@dataclass(frozen=True)
class AdapterContext:
    trace_id: str
    actor_id: str
    device_id: str = "demo-device"
    idempotency_key: str = ""
    # Optional for legacy Mock callers; mandatory and server-bound at P07 boundary.
    subject_id: str = ""
    account_ref: str = ""
    authorization_ref: str = ""
    authorization_version: int = 0
    operation_digest: str = ""


@dataclass(frozen=True)
class AdapterResult:
    ok: bool
    code: str
    message: str
    data: Dict[str, Any]
    trace_id: str
    request_id: str
    idempotency_key: str = ""
    retryable: bool = False


class IdentityProvider(Protocol):
    def verify_subject(self, ctx: AdapterContext, assertion: str) -> AdapterResult: ...


class DeviceRiskProvider(Protocol):
    def assess(self, ctx: AdapterContext, device_attestation: str) -> AdapterResult: ...


class StrongAuthProvider(Protocol):
    def begin(self, ctx: AdapterContext, operation_digest: str, method: str) -> AdapterResult: ...
    def verify(self, ctx: AdapterContext, challenge_id: str, assertion: str, operation_digest: str) -> AdapterResult: ...


class BankCoreAdapter(Protocol):
    """核心账务替换契约；生产实现应由核心/支付网关提供原子幂等语义。"""
    def get_balance(self, ctx: AdapterContext) -> AdapterResult: ...
    def create_transfer(self, ctx: AdapterContext, amount_cents: int, beneficiary_id: str, note: str) -> AdapterResult: ...
    def query_transaction(self, ctx: AdapterContext, transaction_id: str) -> AdapterResult: ...
    def reverse_transfer(self, ctx: AdapterContext, transaction_id: str, reason: str) -> AdapterResult: ...


class CardManagementAdapter(Protocol):
    """卡片管理替换契约；生产实现应接入真实卡管服务并做状态核验。"""
    def report_loss(self, ctx: AdapterContext, card_id: str) -> AdapterResult: ...
    def unreport_loss(self, ctx: AdapterContext, card_id: str) -> AdapterResult: ...
    def query_card(self, ctx: AdapterContext, card_id: str) -> AdapterResult: ...


class PaymentGatewayAdapter(Protocol):
    def submit(self, ctx: AdapterContext, amount_cents: int, beneficiary_id: str) -> AdapterResult: ...


class RiskControlAdapter(Protocol):
    def evaluate(self, ctx: AdapterContext, operation: Dict[str, Any]) -> AdapterResult: ...


class AmlAdapter(Protocol):
    def screen(self, ctx: AdapterContext, operation: Dict[str, Any]) -> AdapterResult: ...


class NotificationAdapter(Protocol):
    def send(self, ctx: AdapterContext, template: str, recipient: str, payload: Dict[str, Any]) -> AdapterResult: ...


class TaskStore(Protocol):
    def put(self, task_id: str, state: Dict[str, Any]) -> AdapterResult: ...
    def get(self, task_id: str) -> AdapterResult: ...
    def update(self, task_id: str, state: Dict[str, Any]) -> AdapterResult: ...


class MessageQueue(Protocol):
    def publish(self, topic: str, payload: Dict[str, Any], message_id: str) -> AdapterResult: ...
    def dead_letter(self, topic: str, payload: Dict[str, Any], reason: str) -> AdapterResult: ...


class AppendOnlyAuditSink(Protocol):
    def append(self, event: Dict[str, Any]) -> AdapterResult: ...


class KmsProvider(Protocol):
    def encrypt_data_key(self, plaintext_key: bytes, key_alias: str) -> AdapterResult: ...
    def decrypt_data_key(self, wrapped_key: bytes, key_alias: str) -> AdapterResult: ...


class FieldEncryptor(Protocol):
    def encrypt(self, value: str, field_name: str) -> str: ...
    def decrypt(self, value: str, field_name: str) -> str: ...


class MockIdentityProvider:
    def verify_subject(self, ctx: AdapterContext, assertion: str) -> AdapterResult:
        return AdapterResult(bool(assertion), "MOCK_IDENTITY_OK" if assertion else "IDENTITY_REQUIRED", "演示身份已确认" if assertion else "需要真实实名身份断言", {}, ctx.trace_id, new_request_id("identity"))


class MockDeviceRiskProvider:
    def assess(self, ctx: AdapterContext, device_attestation: str) -> AdapterResult:
        risk = "low" if device_attestation else "unknown"
        return AdapterResult(True, "MOCK_DEVICE_ASSESSED", "演示设备风险已评估", {"risk_level": risk, "device_id": ctx.device_id}, ctx.trace_id, new_request_id("device"))


class MockStrongAuthProvider:
    """离线结构化凭证 Mock；生产替换为短信/FIDO2/人脸认证服务。

    challenge 绑定 user/device/operation_digest，按策略配置过期且只能 verify 一次。
    """
    TTL_SECONDS = get_policy()["strong_auth_ttl_seconds"]

    def __init__(self):
        self._challenges: Dict[str, Dict[str, Any]] = {}

    def begin(self, ctx: AdapterContext, operation_digest: str, method: str) -> AdapterResult:
        challenge_id = new_request_id("challenge")
        now = time.time()
        self._challenges[challenge_id] = {"actor_id": ctx.actor_id, "device_id": ctx.device_id,
                                          "operation_digest": operation_digest, "method": method,
                                          "created_at": now, "expires_at": now + self.TTL_SECONDS, "used": False}
        return AdapterResult(True, "MOCK_CHALLENGE_CREATED", "已创建演示强认证挑战", {"challenge_id": challenge_id, "method": method, "operation_digest": operation_digest, "expires_at": now + self.TTL_SECONDS}, ctx.trace_id, new_request_id("auth"))

    def verify(self, ctx: AdapterContext, challenge_id: str, assertion: str, operation_digest: str) -> AdapterResult:
        item = self._challenges.get(challenge_id)
        if item is None:
            return AdapterResult(False, "AUTH_INVALID", "认证挑战不存在", {}, ctx.trace_id, new_request_id("auth"))
        if item["used"]:
            return AdapterResult(False, "AUTH_REPLAY", "认证挑战已使用", {"challenge_id": challenge_id}, ctx.trace_id, new_request_id("auth"))
        if time.time() >= item["expires_at"]:
            return AdapterResult(False, "AUTH_EXPIRED", "认证挑战已过期", {"challenge_id": challenge_id}, ctx.trace_id, new_request_id("auth"))
        if (ctx.actor_id != item["actor_id"] or ctx.device_id != item["device_id"] or
                not hmac.compare_digest(operation_digest or "", item["operation_digest"]) or not assertion):
            return AdapterResult(False, "AUTH_BINDING_MISMATCH", "认证绑定不匹配", {"challenge_id": challenge_id}, ctx.trace_id, new_request_id("auth"))
        item["used"] = True
        return AdapterResult(True, "MOCK_AUTH_OK", "演示强认证通过", {"challenge_id": challenge_id, "bound_digest": operation_digest, "used_at": time.time()}, ctx.trace_id, new_request_id("auth"))


class MockCardManagementAdapter:
    """离线卡片管理替身；生产替换点是银行真实卡管/渠道 API。

    ``cards`` 由调用方持有，因此卡片写入与 MockBankCore 的查询共享同一状态，
    不会出现“适配器说已挂失、预检仍显示正常”的双份事实源。
    """

    def __init__(self, cards: List[Dict[str, Any]]):
        self.cards = cards
        self._idempotency: Dict[str, AdapterResult] = {}

    def _result(self, ctx: AdapterContext, code: str, message: str, data: Dict[str, Any], ok: bool = True) -> AdapterResult:
        return AdapterResult(ok, code, message, data, ctx.trace_id, new_request_id("card"), ctx.idempotency_key)

    def _find(self, card_id: str) -> Optional[Dict[str, Any]]:
        return next((card for card in self.cards if card.get("id") == card_id), None)

    def _write(self, ctx: AdapterContext, card_id: str, target_status: str, operation: str) -> AdapterResult:
        key = ctx.idempotency_key or f"card:{ctx.actor_id}:{operation}:{card_id}"
        replay = self._idempotency.get(key)
        if replay is not None:
            data = dict(replay.data, idempotent_replay=True)
            return self._result(ctx, replay.code, replay.message, data, replay.ok)
        card = self._find(card_id)
        if card is None:
            return self._result(ctx, "CARD_NOT_FOUND", "未找到要操作的卡片。", {"card_id": card_id}, False)
        previous = card.get("status")
        if (sum(item.get('id') == card_id for item in self.cards) != 1 or
                previous not in {'normal', 'active', '正常', 'reported_loss', '挂失'}):
            return self._result(ctx, 'CARD_REVIEW_REQUIRED', '卡片标识或状态需核实，本次未修改。', {'card_id':card_id}, False)
        card["status"] = target_status
        data = {"card_id": card_id, "last4": card.get("last4", ""),
                "previous_status": previous, "status": target_status,
                "operation": operation}
        result = self._result(ctx, "CARD_STATUS_UPDATED", "卡片状态已更新。", data)
        self._idempotency[key] = result
        return result

    def report_loss(self, ctx: AdapterContext, card_id: str) -> AdapterResult:
        return self._write(ctx, card_id, "reported_loss", "report_loss")

    def unreport_loss(self, ctx: AdapterContext, card_id: str) -> AdapterResult:
        return self._write(ctx, card_id, "正常", "unreport_loss")

    def query_card(self, ctx: AdapterContext, card_id: str) -> AdapterResult:
        card = self._find(card_id)
        if card is None:
            return self._result(ctx, "CARD_NOT_FOUND", "未找到要查询的卡片。", {"card_id": card_id}, False)
        return self._result(ctx, "CARD_STATUS_OK", "卡片状态已核验。", dict(card))


class MockBankCoreAdapter:
    """离线核心账务适配器；生产替换点是银行核心/支付网关的交易 API。"""

    def __init__(self):
        self._transactions: Dict[str, Dict[str, Any]] = {}
        self._idempotency: Dict[str, str] = {}

    @staticmethod
    def _result(ctx: AdapterContext, transaction: Dict[str, Any], replay: bool = False) -> AdapterResult:
        data = dict(transaction)
        if replay:
            data["idempotent_replay"] = True
        return AdapterResult(True, "MOCK_CORE_OK", "演示核心账务状态已返回", data, ctx.trace_id, new_request_id("core"), ctx.idempotency_key)

    def get_balance(self, ctx: AdapterContext) -> AdapterResult:
        return AdapterResult(True, "MOCK_CORE_OK", "演示核心账务查询成功", {"available_cents": 1288000}, ctx.trace_id, new_request_id("core"))

    def create_transfer(self, ctx: AdapterContext, amount_cents: int, beneficiary_id: str, note: str) -> AdapterResult:
        if type(amount_cents) is not int or amount_cents <= 0:
            return AdapterResult(False, "INVALID_AMOUNT", "金额必须是正整数分", {}, ctx.trace_id, new_request_id("core"), ctx.idempotency_key)
        idempotency_key = ctx.idempotency_key or f"ctx:{ctx.trace_id}:{beneficiary_id}:{amount_cents}:{note}"
        existing_id = self._idempotency.get(idempotency_key)
        if existing_id:
            return self._result(ctx, self._transactions[existing_id], replay=True)
        transaction = {
            "transaction_id": new_request_id("txn"),
            "status": "succeeded",
            "state_history": ["created", "processing", "succeeded"],
            "beneficiary_id": beneficiary_id,
            "amount_cents": amount_cents,
            "note": note,
        }
        self._transactions[transaction["transaction_id"]] = transaction
        self._idempotency[idempotency_key] = transaction["transaction_id"]
        return self._result(ctx, transaction)

    def query_transaction(self, ctx: AdapterContext, transaction_id: str) -> AdapterResult:
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            return AdapterResult(False, "TRANSACTION_NOT_FOUND", "未找到交易", {}, ctx.trace_id, new_request_id("core"), ctx.idempotency_key)
        return self._result(ctx, transaction)

    def reverse_transfer(self, ctx: AdapterContext, transaction_id: str, reason: str) -> AdapterResult:
        transaction = self._transactions.get(transaction_id)
        if transaction is None:
            return AdapterResult(False, "TRANSACTION_NOT_FOUND", "未找到交易", {}, ctx.trace_id, new_request_id("core"), ctx.idempotency_key)
        if transaction["status"] == "reversed":
            return self._result(ctx, transaction, replay=True)
        if transaction["status"] != "succeeded":
            return AdapterResult(False, "INVALID_STATE", "只有成功交易可以冲正", {}, ctx.trace_id, new_request_id("core"), ctx.idempotency_key)
        transaction["status"] = "reversed"
        transaction["state_history"] = transaction["state_history"] + ["reversed"]
        transaction["reversal_reason"] = reason
        return self._result(ctx, transaction)


class NoopExternalAdapter:
    """默认拒绝任何未配置的生产外部系统，避免误把 Demo 当成真实支付。"""

    def __init__(self, name: str):
        self.name = name

    def __getattr__(self, _name: str):
        def reject(*_args: Any, **_kwargs: Any) -> AdapterResult:
            trace_id = _kwargs.get("trace_id", "unbound")
            return AdapterResult(False, "ADAPTER_NOT_CONFIGURED", f"{self.name} 适配器尚未配置真实连接", {}, trace_id, new_request_id("adapter"), retryable=False)
        return reject


class InMemoryTaskStore:
    def __init__(self):
        self._items: Dict[str, Dict[str, Any]] = {}

    def put(self, task_id: str, state: Dict[str, Any]) -> AdapterResult:
        self._items[task_id] = dict(state)
        return AdapterResult(True, "TASK_SAVED", "任务状态已保存", {"task_id": task_id}, state.get("trace_id", ""), new_request_id("task"))

    def get(self, task_id: str) -> AdapterResult:
        state = self._items.get(task_id)
        return AdapterResult(state is not None, "TASK_FOUND" if state else "TASK_NOT_FOUND", "已读取任务状态" if state else "任务不存在", state or {}, state.get("trace_id", "") if state else "", new_request_id("task"))

    def update(self, task_id: str, state: Dict[str, Any]) -> AdapterResult:
        return self.put(task_id, state)


class InMemoryQueue:
    def __init__(self):
        self.messages: List[Dict[str, Any]] = []
        self.dead_letters: List[Dict[str, Any]] = []

    def publish(self, topic: str, payload: Dict[str, Any], message_id: str) -> AdapterResult:
        self.messages.append({"topic": topic, "payload": payload, "message_id": message_id, "published_at": time.time()})
        return AdapterResult(True, "MESSAGE_ENQUEUED", "消息已进入队列", {"message_id": message_id}, payload.get("trace_id", ""), new_request_id("queue"), message_id)

    def dead_letter(self, topic: str, payload: Dict[str, Any], reason: str) -> AdapterResult:
        self.dead_letters.append({"topic": topic, "payload": payload, "reason": reason, "created_at": time.time()})
        return AdapterResult(True, "MESSAGE_DEAD_LETTERED", "消息已进入死信队列", {"reason": reason}, payload.get("trace_id", ""), new_request_id("queue"))


class LocalFieldEncryptor:
    """测试用可逆脱敏器，不是生产密码学实现；生产应调用 KMS/Vault 或 HSM。"""

    def __init__(self, secret: Optional[str] = None):
        self.secret = (secret or os.getenv("FIELD_ENCRYPTION_DEMO_KEY", "local-demo-only")).encode("utf-8")

    def _key(self, field_name: str) -> bytes:
        return hashlib.sha256(self.secret + field_name.encode("utf-8")).digest()

    def encrypt(self, value: str, field_name: str) -> str:
        key = self._key(field_name)
        raw = value.encode("utf-8")
        cipher = bytes(b ^ key[i % len(key)] for i, b in enumerate(raw))
        return "demo:v1:" + base64.urlsafe_b64encode(cipher).decode("ascii")

    def decrypt(self, value: str, field_name: str) -> str:
        if not value.startswith("demo:v1:"):
            raise ValueError("不是本地演示密文")
        key = self._key(field_name)
        raw = base64.urlsafe_b64decode(value[8:].encode("ascii"))
        return bytes(b ^ key[i % len(key)] for i, b in enumerate(raw)).decode("utf-8")


class LocalAppendOnlyAuditSink:
    def __init__(self):
        self.events: List[Dict[str, Any]] = []

    def append(self, event: Dict[str, Any]) -> AdapterResult:
        self.events.append(dict(event))
        return AdapterResult(True, "AUDIT_APPENDED", "审计事件已追加写入", {}, event.get("trace_id", ""), new_request_id("audit"))


def retry_policy() -> Dict[str, Any]:
    return {
        "max_attempts": get_policy()["retry_max_attempts"],
        "backoff": "exponential",
        "base_delay_ms": get_policy()["retry_base_delay_ms"],
        "max_delay_ms": get_policy()["retry_max_delay_ms"],
        "jitter": True,
        "dead_letter_after_exhaustion": True,
        "compensation_required_for_side_effects": True,
        "never_retry": ["create_transfer", "cancel_subscription", "set_family_allowance"],
    }


def production_contract_manifest() -> Dict[str, Any]:
    return {
        "status": "DEMO_CONTRACTS_ONLY",
        "message": "已提供生产替换边界，当前未连接真实银行系统。",
        "identity": ["实名身份断言", "设备指纹和设备风险", "SMS", "Face", "FIDO2/U-Key"],
        "strong_auth_credential": {"bound_fields": ["user_id", "device_id", "task_id", "node_id", "operation_type", "amount_cents", "recipient", "expires_at", "nonce", "used_at"], "ttl_seconds": get_policy()["strong_auth_ttl_seconds"], "single_use": True, "production_replacement": "由认证中心签发带交易摘要、设备和用户绑定的 OTP/FIDO 凭证，并以共享存储原子消费"},
        "external_systems": ["core_banking", "payment_gateway", "risk_control", "aml", "notification"],
        "card_management_contract": {
            "interface": "CardManagementAdapter",
            "operations": ["report_loss", "unreport_loss", "query_card"],
            "mock_boundary": "MockBankCore 卡片状态 normal/reported_loss 的离线演示写操作",
            "production_replacement": "接入真实卡管服务，完成强认证、状态核验、对账、并发一致性和生产审计后才可上线",
        },
        "core_banking_contract": {
            "interface": "BankCoreAdapter",
            "operations": ["get_balance", "create_transfer", "query_transaction", "reverse_transfer"],
            "transaction_states": ["created", "processing", "succeeded", "failed", "reversed"],
            "idempotency": "同一 idempotency_key 必须返回同一 transaction_id 与结果，禁止重复扣款",
            "final_status_rule": "Agent 只依据 query_transaction 的最终状态向用户报告成功/失败",
            "production_replacement": "以银行核心/支付网关正式 SDK 或 mTLS API 实现 BankCoreAdapter；由真实核心负责原子扣账、幂等键持久化、状态查询和冲正",
            "p07_adapter": {"module": "bank_service", "class": "BankServiceAdapter", "profile": "project-owned normalized protocol v1; not a bank API standard",
                            "scope": "LOCAL_PROTOCOL_TEST; actual provider transport/IAM/account authorization remain unconfigured",
                            "context_version": 1, "query_by_idempotency": True, "statement_reconciliation": "read-only discrepancy report",
                            "callback": "optional project-profile HMAC verification; not exposed as a public bank callback route"},
        },
        "state_and_messaging": ["Redis task state", "PostgreSQL task/audit", "message queue", "retry", "compensation", "dead letter queue", "core transaction state machine"],
        "security_operations": ["WORM append-only audit", "SIEM", "KMS/Vault/HSM", "field-level encryption", "audit hash-chain verification", "tamper-triggered high-risk freeze"],
        "audit": {
            "event_schema": [
                "trace_id", "task_id", "user_id", "device_id", "event_type", "operation",
                "risk_level", "decision_reason", "tool_name", "timestamp", "prev_hash", "current_hash",
            ],
            "hash_chain": {
                "algorithm": "SHA-256",
                "scope": "global append order, verified through the requested trace's last sequence",
                "genesis": "GENESIS",
                "verification_runtime": "AuditStore.verify_trace(trace_id)",
                "verification_endpoint": "GET /api/v1/audit/verify?trace_id=...",
            },
            "tamper_response": "检测到断点后发布 security.tamper，并冻结该用户高风险操作；人工解锁使用 POST /api/v1/audit/unlock。",
            "redaction": "审计写入和输出边界统一脱敏手机号、卡号、身份证号和验证码，仅保留头3尾4；六位验证码全量掩码。",
            "production_replacement": {
                "worm": "以银行审计中心或对象存储 WORM/Legal Hold append-only sink 替换 SQLite AuditStore，并由独立服务账号写入。",
                "siem": "将结构化审计事件可靠投递至 SIEM/SOC（Kafka/HTTPS/syslog over mTLS），配置重试、死信、告警和访问审计。",
                "key_management": "由 KMS/Vault/HSM 管理链签名密钥、版本、轮换和吊销；生产可在哈希链之外增加 HMAC/非对称签名，验证公钥与密钥版本随事件留存。",
                "retention_and_access": "按监管留存策略保留原始事件、校验结果和篡改告警；生产查询走最小权限和双人审批。",
            },
        },
        "availability": ["multi-instance", "readiness/liveness", "rate limit", "circuit breaker", "RPO/RTO", "backup restore", "disaster recovery drill"],
        "quality_gates": ["unit/integration/E2E", "load test", "penetration test", "code audit", "dependency/SBOM", "ML red team", "PIPL/privacy review", "MLPS/监管评估", "elder usability", "accessibility", "human support"],
        "retry_policy": retry_policy(),
        "optional_adapters": {"module": "production_backends", "backends": ["RedisTaskStore", "RedisMessageQueue", "PostgresTaskStore", "PostgresAppendOnlyAuditSink"], "dependency_file": "requirements-production.txt", "activation": "显式配置 TASK_STORE_BACKEND/AUDIT_BACKEND 后构造，不静默回退"},
        "preflight": {"endpoint": "/api/preflight", "cli": "scripts/preflight.py", "network_probe_opt_in": True},
        "mock_boundary": {"core": "MockBankCore", "auth": "MockStrongAuthProvider", "audit": "SQLite hash chain", "field_encryption": "LocalFieldEncryptor"},
    }
