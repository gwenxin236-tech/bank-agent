#!/usr/bin/env python3
"""澜银行 AI 智能体演示后端。

本文件是一个无第三方依赖、可直接运行的安全边界参考实现：
- Agent 只负责意图理解与计划编排；
- PolicyEngine 在服务端判定绿/黄/红权限；
- 工具网关只允许注册工具，副作用工具具备幂等键；
- 用户看到自然语言计划，系统和审计保留结构化 JSON；
- 所有事实型回复只从 MockBankCore 的工具结果生成。

当前版本使用 MockBankCore，不连接真实银行核心系统。
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import threading
import uuid
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import parse_qs, urlparse

from llm_adapter import LLMConfig, LLMIntentAdapter
from knowledge_service import KnowledgeService, KnowledgeScope, EmbeddingClient
from ocr_service import OCRService, MAX_IMAGE_BYTES
from speech_adapter import SpeechAdapter, MAX_AUDIO_BYTES, prepare_audio, VoiceEvidenceStore
from tts_output import TTSOutput
from channel_contracts import AgentGateway, UnifiedMessage
from conversation_context import TransferDraftStore
from security_controls import SlidingWindowRateLimiter
from production_contracts import (AdapterContext, MockCardManagementAdapter, production_contract_manifest)
from bank_service import BankServiceAdapter, BankServiceError, ProtocolBankFacade
from operational_readiness import preflight_report
from preflight import build_payment_preflight
from domain_agent_contract import DomainContext
from transaction_agent import TransactionAgent, PaymentSnapshot, AgentMessage
from domain_agent_registry import DOMAIN_AGENT_REGISTRY
from domain_snapshots import tool_snapshot, policy_snapshot, family_snapshot, budget_snapshot
from supervisor import Supervisor, SupervisorError
from transaction_views import dag_receipt, receipt_view, transaction_view
from handoff_contract import HandoffCard
from handoff_queue import HandoffQueue, HandoffConflict
from live_support import LiveSupport, OPERATOR_SCRIPT
from risk_controls import assess_transfer_risk
from notification_bus import NotificationBus
from guardianship import GuardianshipStore
from family_access import FamilyAccess
from identity_service import IdentityService
from recovery import RecoveryMixin, durable_action
from policy_config import get_policy
from feature_registry import FEATURE_INTENT, load_features, load_dag_enabled, CONFIG_PATH as FEATURES_PATH
from task_dag import (TaskDagStore, DagNode, DagExecutor, plan_to_dag, linkage_to_dag,
                      linkage_view, migrate_linkage_to_dag)

_DAG_TOOL_KEY = ContextVar("dag_tool_key", default=None)

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
STATIC_DIR = ROOT / "app" / "static"
DB_PATH = Path(os.getenv("BANK_AGENT_DB_PATH", str(DATA_DIR / "banking_agent.sqlite3")))
FRAUD_RULES_PATH = ROOT / "config" / "fraud_rules.json"
HOST = os.getenv("BANK_AGENT_HOST", "127.0.0.1")
PORT = int(os.getenv("BANK_AGENT_PORT", "8090"))
POLICY = get_policy()  # immutable process snapshot; configuration changes require restart
MAX_AMOUNT_CENTS = POLICY["max_amount_cents"]
TOKEN_TTL_SECONDS = POLICY["consent_ttl_seconds"]
STRONG_AUTH_TTL_SECONDS = POLICY["strong_auth_ttl_seconds"]


def strict_positive_int(value: Any) -> bool:
    """拒绝 bool、float、Decimal 等隐式转型，金额只能是正整数分。"""
    return type(value) is int and value > 0


def strict_non_negative_int(value: Any) -> bool:
    return type(value) is int and value >= 0

RISK_LABELS = {
    "green": {"label": "绿色 · 自动完成", "reason": "这是只读查询，不会改变账户状态。"},
    "yellow": {"label": "黄色 · 需要确认", "reason": "这是可产生业务影响的操作，执行前需要你的明确确认。"},
    "red": {"label": "红色 · 需要强验证", "reason": "这是高风险操作，除明确确认外还需要短信验证码或其他强认证。"},
}

TOOL_LABELS = {
    "knowledge_lookup": "检索已授权知识资料并核对出处",
    "get_balance": "读取账户余额",
    "list_transactions": "读取交易流水",
    "analyze_bills": "整理消费分类与异常摘要",
    "list_investments": "读取理财持仓",
    "list_subscriptions": "读取订阅列表",
    "list_cards": "读取卡片状态",
    "report_card_loss": "挂失卡片",
    "unreport_card_loss": "解除卡片挂失",
    "verify_card_status": "核验卡片最终状态",
    "resolve_beneficiary": "核验收款人",
    "check_transfer_constraints": "检查余额和当日转账额度",
    "create_transfer": "创建转账",
    "cancel_subscription": "取消订阅",
    "clarify": "澄清你的需求",
    "clarify_transfer": "补充转账要素",
    "human_handoff": "转人工核实",
    "detect_fraud_signals": "识别可疑转账风险",
    "create_cooling_off": f"设置{POLICY['cooling_off_hours']}小时冷静期",
    "notify_guardian": "通知守护家人",
    "financial_health_report": "生成财务健康报告",
    "list_savings_goals": "查看攒钱目标",
    "create_savings_goal": "建立攒钱目标",
    "idle_cash_check": "检查是否有闲钱需要安排",
    "list_family_relationships": "查看家庭成员和授权范围",
    "family_auth_check": "核验家庭共管授权",
    "set_family_allowance": "设置家庭零花钱限额",
    "calculate_aa_settlement": "计算家庭 AA 分摊",
    "create_birthday_linkage": "创建生日联动任务",
}

# 工具注册表是执行边界：Planner 可以提出计划，但只能调用此表中的工具。
TOOL_REGISTRY = {
    "knowledge_lookup": {"side_effect": False, "retry": 0},
    "get_balance": {"side_effect": False, "retry": 1},
    "list_transactions": {"side_effect": False, "retry": 1},
    "analyze_bills": {"side_effect": False, "retry": 1},
    "list_investments": {"side_effect": False, "retry": 1},
    "list_subscriptions": {"side_effect": False, "retry": 1},
    "list_cards": {"side_effect": False, "retry": 1},
    "report_card_loss": {"side_effect": True, "retry": 0},
    "unreport_card_loss": {"side_effect": True, "retry": 0},
    "verify_card_status": {"side_effect": False, "retry": 1},
    "resolve_beneficiary": {"side_effect": False, "retry": 1},
    "check_transfer_constraints": {"side_effect": False, "retry": 1},
    "create_transfer": {"side_effect": True, "retry": 0},
    "cancel_subscription": {"side_effect": True, "retry": 0},
    "clarify": {"side_effect": False, "retry": 0},
    "clarify_transfer": {"side_effect": False, "retry": 0},
    "human_handoff": {"side_effect": False, "retry": 0},
    "detect_fraud_signals": {"side_effect": False, "retry": 0},
    "create_cooling_off": {"side_effect": True, "retry": 0},
    "notify_guardian": {"side_effect": True, "retry": 0},
    "financial_health_report": {"side_effect": False, "retry": 1},
    "list_savings_goals": {"side_effect": False, "retry": 1},
    "create_savings_goal": {"side_effect": True, "retry": 0},
    "idle_cash_check": {"side_effect": False, "retry": 1},
    "list_family_relationships": {"side_effect": False, "retry": 1},
    "family_auth_check": {"side_effect": False, "retry": 0},
    "set_family_allowance": {"side_effect": True, "retry": 0},
    "calculate_aa_settlement": {"side_effect": False, "retry": 1},
    "create_birthday_linkage": {"side_effect": True, "retry": 0},
    **{name: {"side_effect": True, "retry": 0} for name in
       ("lock_subaccount", "order_flowers", "order_cake", "compensate_subaccount_lock")},
}


# Local domains add tools without overriding any built-in registration.
FEATURE_REGISTRY = load_features(TOOL_REGISTRY)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def today_key() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def money(cents: int) -> str:
    """Display integer cents exactly; never round or coerce business amounts."""
    if type(cents) is not int:
        raise TypeError("cents must be an integer")
    whole, fraction = divmod(abs(cents), 100)
    sign = "-" if cents < 0 else ""
    return f"¥{sign}{whole:,}.{fraction:02d}"


def yuan(cents: int) -> str:
    return money(cents)[1:].removesuffix(".00") + " 元"


def new_id(prefix: str) -> str:
    # Internal evidence IDs use the C2 non-digit alphabet; do not weaken PII masking.
    token = uuid.uuid4().hex[:12].translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
    return f"{prefix}_{token}"


def json_bytes(data: Any) -> bytes:
    return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")


DEFAULT_FRAUD_RULES = {
    "version": "builtin-safe-default",
    "keyword_rules": [
        {"keyword": "安全账户", "reason": "对方要求转入所谓安全账户"},
        {"keyword": "刷单返现", "reason": "出现刷单返现话术"},
        {"keyword": "刷单", "reason": "出现刷单话术"},
        {"keyword": "返现", "reason": "出现返现诱导话术"},
        {"keyword": "验证码告诉你", "reason": "对方要求提供短信验证码"},
        {"keyword": "投资导师", "reason": "出现冒充投资导师诱导转账话术"},
        {"keyword": "高收益稳赚", "reason": "出现高收益稳赚诱导话术"},
        {"keyword": "保本高收益", "reason": "出现保本高收益诱导话术"},
        {"keyword": "解冻保证金", "reason": "出现以解冻保证金为由要求转账"},
        {"keyword": "解除风控", "reason": "出现以解除风控为由要求转账"},
        {"keyword": "解冻账户", "reason": "出现以解冻账户为由要求转账"},
        {"keyword": "客服要求转账", "reason": "出现冒充客服要求转账"},
        {"keyword": "带你赚", "reason": "出现高收益诱导话术"},
    ],
    "new_recipient_threshold_cents": POLICY["fraud_new_recipient_threshold_cents"],
    "cooling_off_hours": POLICY["cooling_off_hours"],
    "hotline": "96110",
}


def load_fraud_rules(path: Path = FRAUD_RULES_PATH) -> Dict[str, Any]:
    """读取可扩展的反诈规则；配置损坏时保留内置规则，避免安全控制失效。"""
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        loaded = {}
    if not isinstance(loaded, dict):
        loaded = {}

    rules = dict(DEFAULT_FRAUD_RULES)
    keyword_rules = loaded.get("keyword_rules")
    if isinstance(keyword_rules, list):
        valid_rules = [
            item for item in keyword_rules
            if isinstance(item, dict)
            and isinstance(item.get("keyword"), str)
            and item["keyword"].strip()
            and isinstance(item.get("reason"), str)
            and item["reason"].strip()
        ]
        if valid_rules:
            rules["keyword_rules"] = valid_rules
    # Numeric policy cannot be overridden by the extensible phrase corpus.
    if isinstance(loaded.get("hotline"), str) and loaded["hotline"].strip():
        rules["hotline"] = loaded["hotline"].strip()
    rules["source"] = str(path)
    return rules


# ----------------------------- domain models ------------------------------
@dataclass
class ToolResult:
    ok: bool
    tool: str
    data: Dict[str, Any] = field(default_factory=dict)
    error_code: Optional[str] = None
    error_message: Optional[str] = None
    trace_id: str = ""


@dataclass
class PlanStep:
    id: str
    title: str
    tool: str
    status: str = "pending"
    detail: str = ""


@dataclass
class AgentPlan:
    plan_id: str
    trace_id: str
    intent: str
    summary: str
    risk_level: str
    requires_confirmation: bool
    requires_strong_auth: bool
    entities: Dict[str, Any]
    steps: List[PlanStep]
    created_at: str
    consent_token: str = ""
    token_expires_at: str = ""
    actor_id: str = "demo-user"
    entity_digest: str = ""
    device_id: str = "demo-device"
    risk_assessment: Dict[str, Any] = field(default_factory=dict)
    preflight: Dict[str, Any] = field(default_factory=dict)
    coordination: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self, include_token: bool = True) -> Dict[str, Any]:
        """机器视图；审计落盘时必须 include_token=False，避免把操作凭据写入审计。"""
        result = asdict(self)
        result["steps"] = [asdict(x) for x in self.steps]
        if not include_token:
            result.pop("consent_token", None)
        if self.intent == "fraud_guard":
            result["status"] = "cooling_off"
        return result

    def user_view(self) -> Dict[str, Any]:
        """用户视图：只呈现自然语言，不暴露工具名、内部实体结构或原始 JSON。"""
        risk = RISK_LABELS.get(self.risk_level, RISK_LABELS["yellow"])
        public_steps = []
        for index, step in enumerate(self.steps, 1):
            status_label = {"pending": "待执行", "completed": "已完成", "failed": "未完成"}.get(step.status, "处理中")
            description = step.title
            if step.tool == "resolve_beneficiary" and self.entities.get("beneficiary"):
                description = f"确认收款人“{self.entities['beneficiary']}”"
            elif step.tool == "check_transfer_constraints":
                description = "核对可用余额和当日累计额度"
            elif step.tool == "create_transfer":
                description = f"创建向{self.entities.get('beneficiary', '收款人')}的{yuan(self.entities.get('amount_cents') or 0)}转账"
            elif step.tool == "create_birthday_linkage":
                description = f"创建生日联动：即时锁定{yuan(self.entities.get('lock_amount_cents') or 0)}，按日期订购鲜花和蛋糕"
            elif step.tool == "cancel_subscription":
                description = (f"核对并取消{self.entities['subscription_name']}，每期{money(self.entities['subscription_amount_cents'])}"
                               if self.entities.get('subscription_name') else "请先指明要取消的订阅")
            elif step.tool == "clarify_transfer":
                description = "请补充收款人和转账金额"
            elif step.tool == "create_cooling_off":
                hours = self.entities.get("cooling_off_hours", 24)
                description = f"暂停这笔转账{hours}小时，让你有时间核实对方"
            elif step.tool == "notify_guardian":
                description = "把异常转账提醒发送给已授权的守护家人"
            elif step.tool == "financial_health_report":
                description = "整理消费、自动扣费和异常交易，给出下一步建议"
            elif step.tool == "create_savings_goal":
                description = f"建立{self.entities.get('goal_name', '攒钱')}目标"
            elif step.tool == "idle_cash_check":
                description = "看看是否有暂时不用的钱可以安排"
            elif step.tool == "list_family_relationships":
                description = "查看谁可以帮谁办理哪些事情"
            elif step.tool == "family_auth_check":
                description = "确认这次家庭共管操作是否有授权"
            elif step.tool == "set_family_allowance":
                description = f"为{self.entities.get('member', '家人')}设置每月零花钱上限"
            elif step.tool == "calculate_aa_settlement":
                description = "计算本次家庭 AA 需要谁给谁多少钱"
            public_steps.append({
                "index": index,
                "title": TOOL_LABELS.get(step.tool, step.title),
                "description": description,
                "status": step.status,
                "status_label": status_label,
            })

        details: List[str] = []
        if self.intent == "transfer":
            if self.entities.get("beneficiary"):
                details.append(f"收款人：{self.entities['beneficiary']}")
            if self.entities.get("beneficiary_account"):
                details.append(f"演示收款账户（手机号映射）：{self.entities['beneficiary_account']}")
            if self.entities.get("amount_cents"):
                details.append(f"金额：{yuan(self.entities['amount_cents'])}")
            if self.entities.get("note"):
                details.append(f"备注：{self.entities['note']}")
        elif self.intent == "birthday_linkage":
            details.append(f"生日：{self.entities.get('birthday_date') or '待补充'}")
            details.append(f"鲜花提前{self.entities.get('flowers_days_before', 2)}天；蛋糕提前{self.entities.get('cake_days_before', 1)}天")
            if self.entities.get('schedule_error'):
                details.append(self.entities['schedule_error'])
        elif self.intent == "subscription_cancel":
            if self.entities.get('subscription_name'):
                details.append(f"申请停止扣费的订阅：{self.entities['subscription_name']}")
                details.append(f"每期金额：{money(self.entities['subscription_amount_cents'])}（取消不会扣款）")
            details.append("授权层：确认后先核验办理条件；符合条件才提交停止未来扣费申请，结果以回执为准。")
            details.append("服务层：服务可能仍有效到订阅有效期结束；退款金额与状态需单独核验。")
            details.append("交易层：历史扣款可查询；未来少扣的钱不算已到账退款。")
        elif self.intent == "fraud_guard":
            details.append("系统发现这笔转账可能涉及诈骗话术或异常收款人。")
            details.append(f"不会扣钱，先暂停{POLICY['cooling_off_hours']}小时，并提醒已授权的守护家人。")
            for signal in self.entities.get("fraud_signals", []):
                details.append(f"可疑信号：{signal}")
        elif self.intent == "financial_health":
            action = self.entities.get("action", "report")
            if action == "savings_goal":
                details.append(f"目标：{self.entities.get('goal_name', '攒钱目标')}")
                if self.entities.get("target_cents"):
                    details.append(f"目标金额：{yuan(self.entities['target_cents'])}")
                if self.entities.get("monthly_cents"):
                    details.append(f"每月计划：{yuan(self.entities['monthly_cents'])}")
            else:
                details.append("只读取你的消费和扣费情况，不会自动动用账户里的钱。")
        elif self.intent == "family_co_management":
            action = self.entities.get("action", "overview")
            if action == "set_allowance":
                details.append(f"成员：{self.entities.get('member', '家人')}")
                details.append(f"每月上限：{yuan(self.entities.get('allowance_cents') or 0)}")
                details.append("只有得到授权的家庭成员，才能替对方设置这项限额。")
            elif action == "aa_settlement":
                details.append("先计算分摊结果，再由双方确认，不会自动扣款。")
            else:
                details.append("先查看家庭成员关系和每个人可以办理的范围。")
        if self.preflight:
            details.append("支付预检（只读，不代表银行已受理）：")
            details.extend(self.preflight.get("messages", []))

        plain_summary = self.summary
        if self.intent == "balance_query":
            plain_summary = "帮你看看账户里还有多少钱"
        elif self.intent == "transaction_query":
            plain_summary = "帮你看看最近的钱花到哪里了"
        elif self.intent == "bill_analysis":
            plain_summary = "帮你把最近花的钱分分类，并看看有没有不认识的扣款"
        elif self.intent == "investment_query":
            plain_summary = "帮你看看理财里的钱和收益"
        elif self.intent == "subscription_query":
            plain_summary = "帮你看看哪些服务会自动扣钱"
        elif self.intent == "subscription_cancel":
            plain_summary = self.summary
        elif self.intent == "card_manage":
            plain_summary = self.summary
        elif self.intent == "fraud_guard":
            plain_summary = "这笔转账看起来有被骗的风险，我先帮你停下来核实"
        elif self.intent == "financial_health":
            plain_summary = "帮你看看钱花在哪里，并一起安排接下来的目标" if self.entities.get("action") != "savings_goal" else "帮你建立一个攒钱目标，但不会擅自扣钱"
        elif self.intent == "family_co_management":
            plain_summary = "帮你看看家人之间谁可以替谁办理什么" if self.entities.get("action") == "overview" else ("帮你算清楚这次家庭 AA 怎么分" if self.entities.get("action") == "aa_settlement" else "帮你设置家庭零花钱上限，先核对授权")

        return {
            "plan_id": self.plan_id,
            "trace_id": self.trace_id,
            "title": "本次操作计划",
            "summary": self.summary,
            "plain_summary": plain_summary,
            "risk_level": self.risk_level,
            "risk_label": risk["label"],
            "risk_reason": self.risk_assessment.get("decision_reason") or risk["reason"],
            "risk_score": self.risk_assessment.get("risk_score", 0),
            "requires_confirmation": self.requires_confirmation,
            "requires_strong_auth": self.requires_strong_auth,
            "protective_block": self.intent == "fraud_guard",
            "details": details,
            "preflight": self.preflight,
            "steps": public_steps,
            "domain_guidance": self.coordination.get('domain_guidance'),
            "confirmation_hint": "请核对收款人、金额和备注，确认无误后再执行。" if self.intent == "transfer" else "请确认以上操作后再继续。",
            "created_at": self.created_at,
            # 这是绑定当前 plan_id 的一次性操作凭据，前端只作为不可读 opaque token 传回。
            "action_token": self.consent_token,
        }


# ----------------------------- presentation -------------------------------
def card_label(card):
    tail = card.get('last4')
    suffix = f'尾号{tail}' if isinstance(tail, str) and re.fullmatch(r'[0-9]{4}', tail) else '尾号待核实'
    return f"{card.get('name') or '银行卡'}（{suffix}）"


def present_result(plan: AgentPlan, result: Dict[str, Any]) -> Dict[str, Any]:
    """把机器执行结果转换成用户可读文字；不接受模型生成的数字作为事实来源。"""
    if result.get("status") == "PAUSED":
        return {"ok": True, "status": "PAUSED", "title": "任务已暂停",
                "user_message": "任务已暂停，已受理的操作不会撤销；恢复后请核对剩余计划并重新确认。", "trace_id": plan.trace_id}
    if result.get("status") == "DEFERRED":
        return {"ok": False, "status": "DEFERRED", "title": "交易已暂缓", "user_message": result.get("error") or "交易已暂缓，未发生扣款，请联系人工核实。", "trace_id": plan.trace_id}
    receipt = result.get("receipt")
    if (getattr(plan, 'intent', '') in {'subscription_cancel', 'card_manage'} and result.get('status') == 'FAILED' and result.get('error')):
        return {'ok':False, 'status':'FAILED', 'title':'请先核实办理条件',
                'user_message':result['error'],
                'trace_id':plan.trace_id, 'receipt':receipt}
    if isinstance(receipt, dict) and receipt.get("status") in {"UNKNOWN", "PARTIAL", "FAILED", "PROCESSING"}:
        if getattr(plan, "intent", "") == "subscription_cancel" and receipt.get("status") == "UNKNOWN":
            data = next((item.get("data", {}) for item in result.get("results", [])
                         if item.get("tool") == "cancel_subscription"), {})
            return {"ok": True, "status": "UNKNOWN", "title": "订阅取消待处理",
                    "user_message": f"“{data.get('merchant', '该订阅')}”的取消结果待处理：暂时无法确认是否已停止未来扣费；服务有效期和退款状态也需核验，请不要重复提交。",
                    "trace_id": plan.trace_id, "receipt": receipt}
        return receipt_view(receipt, plan.trace_id)
    if result.get("status") == "UNKNOWN":
        unknown_data = next((item.get("data", {}) for item in result.get("results", [])
                             if item.get("tool") in {"create_transfer", "cancel_subscription"}), {"status": "unknown"})
        if plan.intent == "subscription_cancel":
            return {"ok": True, "status": "UNKNOWN", "title": "订阅取消待处理",
                    "user_message": f"“{unknown_data.get('merchant', '该订阅')}”的取消结果待处理：暂时无法确认是否已停止未来扣费；服务有效期和退款状态也需核验，请不要重复提交。",
                    "trace_id": plan.trace_id}
        return transaction_view(unknown_data, plan.trace_id, outcome_unknown=True)
    if not result.get("ok") or result.get("status") == "FAILED":
        message = result.get("error") or "操作没有完成，已停止执行并保留审计记录。"
        return {"ok": False, "status": "FAILED", "title": "操作未完成", "user_message": message, "trace_id": plan.trace_id}

    tool_results = result.get("results") or []
    data_by_tool = {item.get("tool"): item.get("data", {}) for item in tool_results}
    transfer_data = data_by_tool.get("create_transfer", {})
    if plan.intent == "transfer" and (result.get("status") == "PROCESSING" or transfer_data.get("status") == "processing"):
        return {
            "ok": True,
            "status": "PROCESSING",
            "title": "银行正在确认",
            "user_message": "银行还在确认这笔交易，请不要重复操作",
            "trace_id": plan.trace_id,
        }
    if plan.intent == "knowledge_query":
        message = data_by_tool.get("knowledge_lookup", {}).get("answer", "知识资料暂不可用，未生成无来源回答。")
    elif plan.intent == "balance_query":
        data = data_by_tool.get("get_balance", {})
        cents = data.get("available_cents")
        message = f"当前可用余额为 {money(cents)}。" if isinstance(cents, int) else "已读取到账户余额。"
        coverage = data.get('coverage', {})
        if coverage.get('complete') is False:
            message += "仅包含本次授权的模拟活期账户，不代表全部资产；其他银行、未导入账户、理财和锁定资金未计入。"
    elif plan.intent == "transaction_query":
        items = data_by_tool.get("list_transactions", {}).get("items", [])
        if items:
            message = "最近交易：" + "；".join(f"{x.get('merchant', '未知商户')} {money(abs(x.get('amount_cents', 0)))}" for x in items[:5]) + "。"
        else:
            message = "暂未查询到最近交易记录。"
    elif plan.intent == "bill_analysis":
        categories = data_by_tool.get("analyze_bills", {}).get("by_category", {})
        suspicious = data_by_tool.get("analyze_bills", {}).get("suspicious", [])
        category_text = "、".join(f"{k} {money(v)}" for k, v in categories.items()) or "暂无分类数据"
        message = f"近30天消费分类：{category_text}。"
        if suspicious:
            message += f"发现 {len(suspicious)} 笔待核实交易，请在确认商户后处理。"
    elif plan.intent == "investment_query":
        items = data_by_tool.get("list_investments", {}).get("items", [])
        if items:
            message = "当前持仓：" + "；".join(f"{x.get('name', '产品')} 市值 {money(x.get('value_cents', 0))}，收益 {money(x.get('profit_cents', 0))}" for x in items) + "。"
        else:
            message = "当前没有查询到理财持仓。"
    elif plan.intent == "subscription_query":
        items = data_by_tool.get("list_subscriptions", {}).get("items", [])
        active = [x for x in items if x.get("service_layer", {}).get("member_status") == "active"]
        descriptions = []
        for item in active:
            service = item.get("service_layer", {})
            descriptions.append(f"{item.get('merchant', '未命名服务')}（{'、'.join(item.get('governance_markers', []))}；记录中的到期日：{service.get('valid_until', '待核验')}）")
        message = ("查询记录中有 " + str(len(active)) + " 项服务标记为有效：" + "、".join(descriptions)
                   + "。当前使用权益请向服务商核实。") if active else "本次记录中没有标记为有效的服务，实际使用权益请向服务商核实。"
    elif plan.intent == "subscription_cancel":
        data = data_by_tool.get("cancel_subscription", {})
        layers = data.get("layers", {})
        debit = layers.get("debit_authorization", {})
        service = layers.get("service", {})
        refund = layers.get("refund", {})
        message = (f"已确认停止未来扣费（订阅：{data.get('merchant', '该订阅')}）；"
                   f"服务记录中的到期日为{service.get('valid_until', '待核验')}，当前使用权益请向服务商核实；"
                   f"退款状态待核验（金额 {money(refund.get('amount_cents', 0))}，不是已到账退款）。") if data else "订阅取消操作没有返回可核验结果。"
    elif plan.intent == "transfer":
        data = data_by_tool.get("create_transfer", {})
        if data.get("transaction_id"):
            replay = "（检测到重复请求，已返回原交易结果，未重复扣款）" if data.get("idempotent_replay") else ""
            message = f"已向{data.get('beneficiary', plan.entities.get('beneficiary', '收款人'))}转账 {yuan(data.get('amount_cents', plan.entities.get('amount_cents') or 0))}，交易号为 {data['transaction_id']}。{replay}"
            if data.get('service_scope') in {'LOCAL_PROTOCOL_TEST','FINERACT_LOCAL_SIMULATOR'}:
                message=('开源Fineract核心演示：' if data.get('service_scope')=='FINERACT_LOCAL_SIMULATOR' else '本地银行协议演示：')+message.replace('交易号为','本地查询引用为')+'此引用不是正式银行凭证。'
        else:
            message = "转账已完成。"
    elif plan.intent == "birthday_linkage":
        data = data_by_tool.get("create_birthday_linkage", {})
        message = (
            f"已创建生日联动任务 {data.get('task_id', '')}：已锁定 {yuan(data.get('lock_amount_cents', 0))} 到子账户，"
            f"鲜花将在生日前2天、蛋糕将在生日前1天处理；任务支持持久化恢复和取消补偿。"
        )
    elif plan.intent == "card_manage":
        operation = plan.entities.get("card_operation")
        data = data_by_tool.get("verify_card_status", {})
        status = data.get("status")
        no_op = any(item.get('data', {}).get('no_op') for item in tool_results)
        if no_op and status in {'reported_loss', '挂失', '正常', 'normal', 'active'}:
            message = (f"卡片末四位 {data.get('last4', '未知')} 已挂失，无需重复挂失；本次仅核验状态。"
                       if operation == 'report_loss' else
                       f"卡片末四位 {data.get('last4', '未知')} 状态正常，无需解挂；本次仅核验状态。")
        elif operation == "report_loss" and status in {"reported_loss", "挂失"}:
            message = f"卡片末四位 {data.get('last4', '未知')} 已挂失，银行返回的最终状态为已挂失。"
        elif operation == "unreport_loss" and status in {"正常", "normal", "active"}:
            message = f"卡片末四位 {data.get('last4', '未知')} 已解挂，银行返回的最终状态为正常。"
        else:
            cards = data_by_tool.get('list_cards', {}).get('items', [])
            states = {'正常':'正常', 'normal':'正常', 'active':'正常', 'reported_loss':'已挂失', '挂失':'已挂失'}
            message = ('卡片状态：' + '；'.join(f"{card_label(c)}：{states.get(c.get('status'), '待核实')}" for c in cards)
                       + '。本次仅查询，未办理挂失或解挂。') if cards else '本次查询范围内未发现卡片。'
        if data.get('service_scope') in {'LOCAL_PROTOCOL_TEST','FINERACT_LOCAL_SIMULATOR'}:
            message='本地卡管协议演示：'+message.replace('银行返回','服务替身返回')
    elif plan.intent == "fraud_guard":
        data = data_by_tool.get("create_cooling_off", {})
        until = data.get("cooling_off_until", "以银行核实结果为准")
        hours = data.get("cooling_off_hours", plan.entities.get("cooling_off_hours", 24))
        hotline = plan.entities.get("fraud_hotline", "96110")
        alert = data_by_tool.get("notify_guardian", {})
        notification_note = "已把提醒放入本地演示通知队列" if alert.get("status") == "LOCAL_QUEUED" else "未连接真实通知平台"
        signals = plan.entities.get("fraud_signals", [])
        signal_text = "、".join(signals[:3]) if signals else "可疑转账特征"
        message = (
            f"我识别到这笔转账可能涉及典型诈骗手法（{signal_text}）。"
            f"我先不帮你转账，这笔钱不会扣钱，任务已标记为 cooling_off，冷静期 {hours}小时，暂停到 {until}；"
            f"{notification_note}。请立即停止转账，建议拨打 {hotline} 核实。"
        )
    elif plan.intent == "financial_health":
        action = plan.entities.get("action", "report")
        if action == "savings_goal":
            data = data_by_tool.get("create_savings_goal", {})
            message = f"已建立“{data.get('goal_name', plan.entities.get('goal_name', '攒钱目标'))}”，目标 {yuan(data.get('target_cents', plan.entities.get('target_cents') or 0))}。目前只记录计划，不会自动扣款。"
        elif action == "idle_cash":
            data = data_by_tool.get("idle_cash_check", {})
            message = data.get("message", "已检查账户中的闲置资金。")
        else:
            data = data_by_tool.get("financial_health_report", {})
            consumer = data.get("consumer_view", {})
            cash_flow = data.get("cash_flow_view", {})
            funds = data.get("funds_status", {})
            coverage = data.get("coverage", {})
            if consumer and cash_flow:
                message = (
                    f"这个月实际消费 {yuan(consumer.get('total_cents', 0))}；"
                    f"这张卡资金流出 {yuan(cash_flow.get('outflow_cents', 0))}（{cash_flow.get('label', '包含还款，不等于消费')}）。"
                    f"处理中 {yuan(funds.get('processing_cents', 0))}，受限/冻结 {yuan(funds.get('restricted_cents', 0))}，"
                    f"预计到账 {yuan(funds.get('expected_arrival_cents', 0))}。"
                    f"{coverage.get('message', '覆盖范围待核验')}"
                )
            else:
                message = data.get("summary", "已生成财务健康报告。")
    elif plan.intent == "family_co_management":
        action = plan.entities.get("action", "overview")
        if action == "set_allowance":
            data = data_by_tool.get("set_family_allowance", {})
            message = f"已为{data.get('member', plan.entities.get('member', '家人'))}设置每月零花钱上限 {yuan(data.get('allowance_cents', plan.entities.get('allowance_cents') or 0))}。"
        elif action == "aa_settlement":
            data = data_by_tool.get("calculate_aa_settlement", {})
            message = data.get("summary", "已算好家庭 AA 分摊结果，双方确认后再办理。")
        else:
            data = data_by_tool.get("list_family_relationships", {})
            message = data.get("summary", "已读取家庭成员和授权范围。")
    else:
        message = "请求已按计划处理。"
    return {"ok": True, "status": "SUCCESS", "title": "操作已完成", "user_message": message, "trace_id": plan.trace_id}


# C7: trusted configuration affects display text only, never machine facts or policy.
class PlainLanguage:
    FALLBACK = {"触发增强认证策略": "为保障您的账户安全，请完成身份验证", "强验证": "身份验证", "强认证": "身份验证"}

    def __init__(self, path: Optional[Path] = None):
        self.configuration_status = "loaded"
        try:
            data = json.loads((path or ROOT / "config/plain_language.json").read_text(encoding="utf-8"))
            terms = data["terms"]
            if not isinstance(terms, dict) or not terms or len(terms) > 500:
                raise ValueError("Invalid terms")
            for source, target in terms.items():
                if (not isinstance(source, str) or not isinstance(target, str) or not 2 <= len(source) <= 160
                        or not 1 <= len(target) <= 240 or re.search(r"[\d¥￥%]", source + target)):
                    raise ValueError("Terms must not replace or invent numeric facts")
            self.terms = terms
        except (OSError, ValueError, KeyError, TypeError):
            self.terms = dict(self.FALLBACK)
            self.configuration_status = "fallback"

    def convert(self, text: str, protected=()) -> str:
        # Recipients and numeric runs match first; one pass prevents cascading replacements.
        literals = [re.escape(x) for x in protected if isinstance(x, str) and x]
        protected_pattern = "|".join(sorted(literals, key=len, reverse=True) + [r"[0-9０-９][0-9０-９,，.．:：/＋+%％\-]*"])
        terms_pattern = "|".join(re.escape(k) for k in sorted(self.terms, key=len, reverse=True))
        pattern = "(?P<protected>" + protected_pattern + ")|(?P<term>" + terms_pattern + ")"
        return re.sub(pattern, lambda m: m.group(0) if m.group("protected") is not None else self.terms[m.group(0)], text)


def present_plain_response(response: Dict[str, Any], enabled: bool, config_path: Optional[Path] = None) -> Dict[str, Any]:
    """HTTP presentation boundary. Original plan/results, auth tokens and audit remain untouched."""
    out = copy.deepcopy(response)
    layer = PlainLanguage(config_path)
    standard = {k: copy.deepcopy(response[k]) for k in ("message", "plan_view", "result_view") if k in response}
    plain = copy.deepcopy(standard)
    entities = response.get("plan", {}).get("entities", {})
    protected = [entities.get(k, "") for k in ("beneficiary", "recipient", "member", "note")]
    if response.get('plan', {}).get('intent') == 'knowledge_query':
        # Quotations stay verbatim even when surrounding UI uses plain language.
        for item in response.get('result', {}).get('results', []):
            if item.get('tool') == 'knowledge_lookup':
                protected.append(item.get('data', {}).get('answer', ''))
    text_fields = {"message", "title", "user_message", "summary", "plain_summary", "risk_reason",
                   "confirmation_hint", "description", "status_label"}
    def convert_view(value, key=""):
        if isinstance(value, dict):
            return {k: convert_view(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [convert_view(v, key) for v in value]
        if isinstance(value, str) and (key in text_fields or key == "details"):
            return layer.convert(value, protected)
        return value
    plain = convert_view(plain)
    plan = response.get("plan", {})
    fraud = plan.get("intent") == "fraud_guard"
    warning = None
    if fraud:
        warning = {"title": "请先不要转账", "explanation": "这笔转账有被骗的风险，已停止继续转账。请先和银行核实，不要按陌生人的要求操作，也不要告诉别人验证码。",
                   "official_help": "请通过银行卡背面或银行官方 App 查找官方客服电话，不要拨打陌生人提供的号码。",
                   "hotline_help": "如有诈骗疑虑，可拨打 96110 咨询。"}
        plain["message"] = warning["title"] + "。\n" + plain.get("message", "") + "\n" + warning["official_help"] + warning["hotline_help"]
    if enabled is True:
        out.update(plain)
    out["presentation"] = {"elder_mode": enabled is True, "plain_language_status": layer.configuration_status,
                           "standard": standard, "plain": plain, "fraud_warning": warning}
    return out


# ----------------------------- audit + safety -----------------------------
def _mask_digits(match: re.Match[str]) -> str:
    value = match.group(0)
    if len(value) == 6:
        return "*" * len(value)
    return value[:3] + ("*" * (len(value) - 7)) + value[-4:]


class _VoiceAuditIdentifier(str):
    """Only server-created identifiers; JSON input cannot carry this Python type."""


def voice_audit_evidence(evidence):
    result=dict(evidence)
    for key,value in result.items():
        pattern=(r'asr_[0-9a-f]{32}' if key=='provider_request_id' else r'trace_[0-9a-f]{32}' if key=='source_trace_id' else
                 r'[0-9a-f]{64}' if key in {'upload_sha256','pcm_sha256','normalized_wav_sha256'} else None)
        if pattern and isinstance(value,str) and re.fullmatch(pattern,value):result[key]=_VoiceAuditIdentifier(value)
    return result


def sanitize_audit_value(value: Any, key: str = "") -> Any:
    """审计边界脱敏：手机号/卡号/证件号/验证码不以完整明文落盘或输出。"""
    if type(value) is _VoiceAuditIdentifier:
        pattern=(r'asr_[0-9a-f]{32}' if key=='provider_request_id' else r'trace_[0-9a-f]{32}' if key=='source_trace_id' else
                 r'[0-9a-f]{64}' if key in {'upload_sha256','pcm_sha256','normalized_wav_sha256'} else None)
        if pattern and re.fullmatch(pattern,value):return str(value)
    sensitive_key = any(token in key.casefold() for token in (
        "phone", "mobile", "card", "account", "identity", "id_card", "id_number", "身份证",
        "otp", "verification", "code", "验证码",
    ))
    if isinstance(value, dict):
        return {str(k): sanitize_audit_value(v, str(k)) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_audit_value(v, key) for v in value]
    if isinstance(value, tuple):
        return [sanitize_audit_value(v, key) for v in value]
    if isinstance(value, int) and not isinstance(value, bool) and sensitive_key:
        digits = str(abs(value))
        return "*" * len(digits)
    if isinstance(value, str):
        if sensitive_key:
            value = re.sub(r"(?<!\d)\d{6,19}(?!\d)", _mask_digits, value)
        return re.sub(r"(?<!\d)\d{6,19}(?!\d)", _mask_digits, value)
    return value


def audit_action(method):
    """Trusted adapter identity for this call only; never an authorization decision.

    The plan's legacy actor_id denotes the account subject. Family authentication
    stays bound to that subject while the evidence names the actual caller.
    """
    signature = inspect.signature(method)

    @wraps(method)
    def wrapped(self, *args, **kwargs):
        params = signature.bind(self, *args, **kwargs)
        params.apply_defaults()
        values = params.arguments
        if method.__name__ == "chat":
            subject = values["actor_id"]
            family = values.get("family_context") or {}
            actor = family.get("guardian_id", subject)
            device = family.get("guardian_device_id", values["device_id"])
        else:
            task_id = values["plan_id"]
            plan = self.pending.get(task_id) or self.auth_plan_snapshots.get(task_id)
            subject = plan.actor_id if plan else values["actor_id"] or "unknown-user"
            # Defaults serve the offline internal API only; HTTP requires a session.
            actor = values["actor_id"] or subject
            device = values["device_id"] or (plan.device_id if plan else "unknown-device")
        with self.audit.identity(actor, subject, device):
            if getattr(self,'bank_service',None) is not None:
                if actor!=subject:return {'ok':False,'error_code':'BANK_DELEGATION_UNSUPPORTED','message':'银行协议演示尚不支持家庭代办。'}
                try:
                    with self.bank_service.scope(self.bank_service.context(actor,device_id=device)):
                        return method(self, *args, **kwargs)
                except BankServiceError:
                    return {'ok':False,'error_code':'BANK_READ_UNAVAILABLE','message':'银行协议服务暂不可核验，未回落模拟成功。'}
            return method(self, *args, **kwargs)
    return wrapped


class AuditStore:
    @contextmanager
    def identity(self, actor, subject, device):
        token = self._identity.set({"actor_id": actor, "subject_id": subject, "device_id": device})
        try:
            yield
        finally:
            self._identity.reset(token)

    def __init__(self, db_path: Path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._identity = ContextVar("audit_request_identity", default=None)
        self.db = sqlite3.connect(db_path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trace_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                event_type TEXT NOT NULL,
                risk_level TEXT,
                intent TEXT,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                seq INTEGER,
                prev_hash TEXT,
                event_hash TEXT,
                current_hash TEXT,
                task_id TEXT,
                user_id TEXT,
                device_id TEXT,
                operation TEXT,
                decision_reason TEXT,
                tool_name TEXT,
                timestamp TEXT
            )"""
        )
        # 兼容旧版演示数据库，避免升级时丢失审计记录。
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(audit_events)").fetchall()}
        if "seq" not in columns:
            self.db.execute("ALTER TABLE audit_events ADD COLUMN seq INTEGER")
        if "prev_hash" not in columns:
            self.db.execute("ALTER TABLE audit_events ADD COLUMN prev_hash TEXT")
        if "event_hash" not in columns:
            self.db.execute("ALTER TABLE audit_events ADD COLUMN event_hash TEXT")
        for name, definition in (("current_hash", "TEXT"), ("task_id", "TEXT"), ("user_id", "TEXT"),
                                 ("device_id", "TEXT"), ("operation", "TEXT"), ("decision_reason", "TEXT"),
                                 ("tool_name", "TEXT"), ("timestamp", "TEXT")):
            if name not in columns:
                self.db.execute(f"ALTER TABLE audit_events ADD COLUMN {name} {definition}")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS linkage_tasks (
                task_id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                actor TEXT NOT NULL,
                status TEXT NOT NULL,
                state_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self.db.commit()

    def save_linkage_task(self, state: Dict[str, Any]) -> None:
        now = now_iso()
        with self.lock:
            try:
                self.db.execute(
                    """INSERT INTO linkage_tasks(task_id, trace_id, actor, status, state_json, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?)
                       ON CONFLICT(task_id) DO UPDATE SET status=excluded.status, state_json=excluded.state_json, updated_at=excluded.updated_at""",
                    (
                        state["task_id"], state["trace_id"], state["actor_id"], state["status"],
                        json.dumps(state, ensure_ascii=False), state.get("created_at", now), now,
                    ),
                )
                store = getattr(self, "linkage_dag_store", None)
                if store is not None:
                    store.sync_linkage(state, commit=False)
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def get_linkage_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self.lock:
            row = self.db.execute("SELECT state_json FROM linkage_tasks WHERE task_id = ?", (task_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def list_linkage_tasks(self) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT state_json FROM linkage_tasks ORDER BY created_at ASC").fetchall()
        return [json.loads(row[0]) for row in rows]

    @staticmethod
    def _hash_body(event: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "seq": event["seq"], "trace_id": event["trace_id"], "task_id": event["task_id"],
            "user_id": event["user_id"], "device_id": event["device_id"], "event_type": event["event_type"],
            "operation": event["operation"], "risk_level": event["risk_level"],
            "decision_reason": event["decision_reason"], "tool_name": event["tool_name"],
            "timestamp": event["timestamp"], "payload": event["payload"], "prev_hash": event["prev_hash"],
        }

    @classmethod
    def _hash_event(cls, event: Dict[str, Any]) -> str:
        return hashlib.sha256(json.dumps(cls._hash_body(event), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

    def write(self, trace_id: str, event_type: str, payload: Dict[str, Any], actor: str = "", risk_level: str = "", intent: str = "", *, commit: bool = True) -> None:
        with self.lock:
            row = self.db.execute("SELECT seq, current_hash, event_hash FROM audit_events WHERE seq IS NOT NULL ORDER BY seq DESC LIMIT 1").fetchone()
            seq = int(row[0] or 0) + 1 if row else 1
            prev_hash = (row[1] or row[2]) if row else None
            prev_hash = prev_hash or "GENESIS"
            prior_identity = self.db.execute(
                "SELECT user_id, device_id, actor FROM audit_events WHERE trace_id = ? ORDER BY seq DESC, id DESC LIMIT 1",
                (trace_id,),
            ).fetchone()
            clean_payload = sanitize_audit_value(payload)
            identity = sanitize_audit_value(self._identity.get() or {})
            clean_actor = identity.get("actor_id") or sanitize_audit_value(str(actor), "actor")
            step = clean_payload.get("step") if isinstance(clean_payload, dict) else None
            task_id = str(clean_payload.get("task_id") or clean_payload.get("plan_id") or trace_id) if isinstance(clean_payload, dict) else trace_id
            plan_payload = clean_payload.get("plan") if isinstance(clean_payload, dict) else None
            plan_actor = plan_payload.get("actor_id") if isinstance(plan_payload, dict) else None
            plan_device = plan_payload.get("device_id") if isinstance(plan_payload, dict) else None
            inherited_user = str(prior_identity[0]) if prior_identity and prior_identity[0] else ""
            inherited_device = str(prior_identity[1]) if prior_identity and prior_identity[1] else ""
            # Subject and caller are distinct, including rejected cross-user calls.
            user_id = str(identity.get("subject_id") or clean_payload.get("subject_id") or clean_payload.get("user_id")
                          or plan_actor or inherited_user or clean_actor or "unknown-user")
            device_id = str(identity.get("device_id") or clean_payload.get("device_id") or plan_device or inherited_device or "unknown-device")
            clean_actor = clean_actor or (prior_identity[2] if prior_identity else "system")
            # Signed-by-hash identity envelope adds integrity without rewriting old chains.
            # Never trust an incoming payload's reserved envelope.
            clean_payload["_identity"] = {"actor_id": clean_actor, "subject_id": user_id, "device_id": device_id}
            operation = str(clean_payload.get("operation") or intent or "") if isinstance(clean_payload, dict) else (intent or "")
            decision_reason = ""
            if isinstance(clean_payload, dict):
                decision_reason = str(clean_payload.get("decision_reason") or clean_payload.get("reason") or clean_payload.get("message") or clean_payload.get("error_message") or "")
            tool_name = ""
            if isinstance(clean_payload, dict):
                tool_name = str(clean_payload.get("tool_name") or clean_payload.get("tool") or (step or {}).get("tool", ""))
            timestamp = now_iso()
            event = {"seq": seq, "trace_id": trace_id, "task_id": task_id, "user_id": user_id,
                     "device_id": device_id, "event_type": event_type, "operation": operation,
                     "risk_level": risk_level, "decision_reason": decision_reason, "tool_name": tool_name,
                     "timestamp": timestamp, "payload": clean_payload, "prev_hash": prev_hash}
            digest = self._hash_event(event)
            self.db.execute(
                """INSERT INTO audit_events(trace_id,actor,event_type,risk_level,intent,payload_json,created_at,seq,prev_hash,event_hash,current_hash,task_id,user_id,device_id,operation,decision_reason,tool_name,timestamp)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (trace_id, clean_actor, event_type, risk_level, intent, json.dumps(clean_payload, ensure_ascii=False), timestamp,
                 seq, prev_hash, digest, digest, task_id, user_id, device_id, operation, decision_reason, tool_name, timestamp),
            )
            if commit:
                self.db.commit()

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> Dict[str, Any]:
        current_hash = row["current_hash"] if "current_hash" in row.keys() else None
        current_hash = current_hash or row["event_hash"]
        timestamp = row["timestamp"] if "timestamp" in row.keys() else None
        timestamp = timestamp or row["created_at"]
        return {
            "id": row["id"], "trace_id": row["trace_id"], "actor": row["actor"],
            "subject_id": row["user_id"], "event_type": row["event_type"],
            "risk_level": row["risk_level"], "intent": row["intent"], "payload": json.loads(row["payload_json"]),
            "created_at": row["created_at"], "timestamp": timestamp, "seq": row["seq"], "prev_hash": row["prev_hash"],
            "event_hash": row["event_hash"], "current_hash": current_hash,
            "task_id": row["task_id"] if "task_id" in row.keys() else row["trace_id"],
            "user_id": row["user_id"] if "user_id" in row.keys() else row["actor"],
            "device_id": row["device_id"] if "device_id" in row.keys() else "unknown-device",
            "operation": row["operation"] if "operation" in row.keys() else row["intent"],
            "decision_reason": row["decision_reason"] if "decision_reason" in row.keys() else "",
            "tool_name": row["tool_name"] if "tool_name" in row.keys() else "",
        }

    def list(self, limit: int = 50) -> List[Dict[str, Any]]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM audit_events ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._event_from_row(row) for row in rows]

    def list_for_trace(self, trace_id: str) -> List[Dict[str, Any]]:
        """返回单个任务的完整审计链；回放必须按全局 seq 正序，而非按最新事件倒序。"""
        with self.lock:
            rows = self.db.execute(
                """SELECT * FROM audit_events
                   WHERE trace_id = ?
                   ORDER BY CASE WHEN seq IS NULL THEN 1 ELSE 0 END, seq ASC, id ASC""",
                (trace_id,),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def verify_trace(self, trace_id: str) -> Dict[str, Any]:
        with self.lock:
            rows = self.db.execute("SELECT * FROM audit_events WHERE seq IS NOT NULL ORDER BY seq ASC, id ASC").fetchall()
        target = [row for row in rows if row["trace_id"] == trace_id]
        if not target:
            return {"ok": True, "valid": True, "trace_id": trace_id, "count": 0, "break_at": None}
        # 不以待校验的单行 user_id 作为冻结依据：若恰好篡改了身份字段，
        # 通过同一 trace 的多数事件和旧版 actor 列恢复最可信的关联用户。
        user_counts: Dict[str, int] = {}
        actor_counts: Dict[str, int] = {}
        for row in target:
            legacy = "_identity" not in json.loads(row["payload_json"])
            for key, counts in (("user_id", user_counts),) + ((("actor", actor_counts),) if legacy else ()):
                value = str(row[key] or "").strip()
                if value:
                    counts[value] = counts.get(value, 0) + 1
        trusted_user = max(user_counts, key=user_counts.get) if user_counts else ""
        if actor_counts:
            trusted_actor = max(actor_counts, key=actor_counts.get)
            if actor_counts[trusted_actor] > user_counts.get(trusted_user, 0):
                trusted_user = trusted_actor
        max_seq = max(int(row["seq"]) for row in target)
        previous_hash = "GENESIS"
        for row in rows:
            if int(row["seq"]) > max_seq:
                break
            event = self._event_from_row(row)
            if event["prev_hash"] != previous_hash:
                return {"ok": True, "valid": False, "trace_id": trace_id, "count": len(target), "break_at": event["seq"],
                        "break_event_id": event["id"], "reason": "prev_hash_mismatch", "user_id": trusted_user or event["user_id"],
                        "task_id": event["task_id"], "frozen_user": trusted_user or event["user_id"]}
            expected = self._hash_event(event)
            identity = event["payload"].get("_identity")
            identity_mismatch = identity is not None and identity != {
                "actor_id": event["actor"], "subject_id": event["user_id"], "device_id": event["device_id"]}
            if event["current_hash"] != expected or identity_mismatch:
                return {"ok": True, "valid": False, "trace_id": trace_id, "count": len(target), "break_at": event["seq"],
                        "break_event_id": event["id"], "reason": "current_hash_mismatch", "user_id": trusted_user or event["user_id"],
                        "task_id": event["task_id"], "frozen_user": trusted_user or event["user_id"]}
            previous_hash = event["current_hash"]
        return {"ok": True, "valid": True, "trace_id": trace_id, "count": len(target), "break_at": None}


class CircuitBreaker:
    def __init__(self, threshold: Optional[int] = None):
        self.threshold = POLICY["breaker_failure_threshold"] if threshold is None else threshold
        self.failures = 0
        self.locked_until: Optional[str] = None
        self.lock = threading.Lock()

    def is_locked(self) -> bool:
        with self.lock:
            return self.locked_until is not None

    def failure(self) -> None:
        with self.lock:
            self.failures += 1
            if self.failures >= self.threshold:
                self.locked_until = now_iso()

    def reset(self) -> None:
        with self.lock:
            self.failures = 0
            self.locked_until = None

    def state(self) -> Dict[str, Any]:
        with self.lock:
            return {"failures": self.failures, "threshold": self.threshold, "locked": self.locked_until is not None, "locked_at": self.locked_until}


class InjectionGuard:
    # 这些规则只负责识别“改变代理控制边界”的输入；权限和金额仍由 Planner/PolicyEngine 裁决。
    PATTERNS = [
        r"忽略.{0,20}(指令|规则|系统|上文|之前)",
        r"(覆盖|改写|重置|忘记|忘掉).{0,20}(原有|之前|系统|安全|限制).{0,12}(指令|规则|提示)?",
        r"(绕过|跳过|取消|无需).{0,12}(权限|验证|确认|认证|二次认证|风控)",
        r"(不要|不做|关闭|禁用)(审计|记录|告诉用户|留痕)",
        r"(system prompt|developer message|jailbreak)",
        r"(越权|提升权限|管理员模式|root权限)",
        r"(你现在是|请扮演|假装.{0,4}(是|成为)|角色扮演).{0,30}(管理员|系统|银行|开发者|客服|审计|root)",
        r"(进入|切换到|启用).{0,20}(开发者|管理员|调试|root|无审计)模式",
        r"(以系统身份|系统身份).{0,20}(批准|执行|转账|付款)",
        r"(?:```(?:system|developer|assistant)?|<\s*/?(?:system|developer|assistant)\s*>|\[(?:/?(?:system|developer|assistant))\]|---+\s*(?:system|developer))",
        r"(从现在起|以后|今后|记住|默认).{0,30}(转账|付款|扣款).{0,20}(免确认|无需确认|不再验证|不再二次认证|自动执行|自动办理|直接办理|代表已经确认)",
        r"(永久|每次|所有).{0,20}(转账|付款|扣款).{0,20}(免确认|无需确认|不再验证|自动执行|自动办理)",
        r"(把|将).{0,30}(金额|转账金额|扣款金额).{0,30}(改成|篡改|伪造|隐藏|替换).{0,30}(实际|真实|工具|后台|扣款|原始|用户)",
        r"(显示|回复|告诉用户|工具返回).{0,20}(100|0\.01|一百|小额).{0,20}(实际|真正|后台|扣款|原始).{0,20}(1000|10000|大额|另一笔)",
        r"(修改|篡改|伪造|隐藏|替换).{0,20}(金额|收款人|交易结果|工具返回|真实金额)",
    ]

    @classmethod
    def inspect(cls, text: str) -> Tuple[bool, Optional[str]]:
        lowered = text.lower()
        if any(re.search(pattern, lowered, flags=re.IGNORECASE) for pattern in cls.PATTERNS):
            return False, "检测到疑似提示注入或越权指令，已拒绝执行。"
        return True, None


# ------------------------------ bank mock core ----------------------------
class MockBankCore:
    """可恢复的离线核心账务替身；生产替换点是核心/支付网关 Adapter。"""

    INITIAL_BALANCE_CENTS = 26_853_000
    TRANSACTION_STATUSES = {"created", "processing", "succeeded", "failed", "reversed"}
    ALLOWED_TRANSITIONS = {
        "created": {"processing", "failed"},
        "processing": {"succeeded", "failed"},
        "succeeded": {"reversed"},
        "failed": set(),
        "reversed": set(),
    }

    def __init__(self, fraud_rules: Optional[Dict[str, Any]] = None, db_path: Optional[Path] = None, fault_injection: Optional[str] = None):
        self.lock = threading.RLock()
        self.fraud_rules = fraud_rules or load_fraud_rules()
        # 可信客户档案与时钟替身；生产替换为实名客户/风险特征服务。
        self.user_profiles: Dict[str, Dict[str, Any]] = {"demo-user": {"is_elderly": False}}
        self.risk_clock = lambda: datetime.now(timezone.utc)
        self.fault_injection = fault_injection
        self.db_path = Path(db_path) if db_path is not None else None
        self.db = sqlite3.connect(str(db_path) if db_path is not None else ":memory:", check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.guardianships = GuardianshipStore(db_path)
        self.guardianships.mutation_lock = self.lock
        self.user_profiles.update({uid: {"is_elderly": a["is_elderly"]} for uid, a in self.guardianships.accounts.items()})
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS core_transactions (
                transaction_id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                amount_cents INTEGER NOT NULL,
                beneficiary TEXT NOT NULL,
                note TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                error_code TEXT,
                error_message TEXT,
                debit_applied INTEGER NOT NULL DEFAULT 0,
                state_history_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )"""
        )
        self.db.commit()
        self.balance_cents = self.INITIAL_BALANCE_CENTS
        self.daily_transfer_cents = 0
        self.daily_transfer_date = today_key()
        self.idempotency_results: Dict[str, ToolResult] = {}
        self.transactions = [
            {"id": "tx_1001", "date": "2026-09-17", "merchant": "星巴克", "category": "餐饮", "amount_cents": -4600},
            {"id": "tx_1002", "date": "2026-09-16", "merchant": "滴滴出行", "category": "交通", "amount_cents": -3200},
            {"id": "tx_1003", "date": "2026-09-15", "merchant": "工资入账", "category": "收入", "amount_cents": 3200000},
            {"id": "tx_1004", "date": "2026-09-14", "merchant": "某视频会员", "category": "订阅", "amount_cents": -2500},
            {"id": "tx_1005", "date": "2026-09-12", "merchant": "陌生境外商户", "category": "异常待核实", "amount_cents": -88800},
        ]
        self.beneficiaries = {"妈妈": "13800138000", "李明": "13900139000", "张伟": "13600136000"}
        # 同名收款人必须先消歧；候选只展示脱敏后四位，绝不由模型或核心猜选。
        # 生产替换点：真实收款人目录/实名核验服务返回候选及展示字段。
        self.beneficiary_candidates = {
            "小王": [
                {"beneficiary_id": "beneficiary_xiaowang_01", "name": "小王", "phone": "13700138001"},
                {"beneficiary_id": "beneficiary_xiaowang_02", "name": "小王", "phone": "13700138002"},
            ],
        }
        self.cards = [{"id": "card_001", "name": "银联白金卡", "last4": "6222", "status": "正常", "limit_cents": 5000000}]
        # 卡片写操作与查询共享同一 MockBankCore 状态；生产替换为真实卡管 Adapter。
        self.card_adapter = MockCardManagementAdapter(self.cards)
        self.subscriptions = [
            {
                "id": "sub_001", "merchant": "某视频会员", "next_charge": "2026-10-14",
                "amount_cents": 2500, "status": "active",
                "transaction_layer": {"history": [{"transaction_id": "tx_1004", "amount_cents": 2500, "status": "settled", "date": "2026-09-14"}]},
                "authorization_layer": {"authorized_subject": "demo-user", "debit_permission": "active", "terminable": True},
                "service_layer": {"member_status": "active", "valid_until": "2026-10-14", "exit_conditions": "停止代扣不等于立即终止服务", "refund_status": "unknown", "refund_amount_cents": 0},
                "governance_markers": ["已确认订阅", "当前渠道可处理"],
            },
            {
                "id": "sub_002", "merchant": "云盘服务", "next_charge": "2026-09-24",
                "amount_cents": 1800, "status": "active",
                "transaction_layer": {"history": []},
                "authorization_layer": {"authorized_subject": "demo-user", "debit_permission": "unknown", "terminable": False},
                "service_layer": {"member_status": "unknown", "valid_until": "2026-09-24", "exit_conditions": "需服务商核实退出条件", "refund_status": "unknown", "refund_amount_cents": 0},
                "governance_markers": ["疑似周期扣费", "需服务商处理", "待核验"],
            },
        ]
        # 结构化演示承诺；生产替换为核心账务/账单系统中已确认的未来应付项查询。
        self.confirmed_commitments = [
            {"id": "commitment_rent_2026_09", "name": "本月房租", "amount_cents": 800000, "status": "confirmed"},
            {"id": "commitment_card_2026_09", "name": "信用卡还款", "amount_cents": 300000, "status": "confirmed"},
        ]
        self.investments = [{"name": "稳健现金管理", "value_cents": 8000000, "profit_cents": 186500, "risk": "R2"}]
        self.savings_goals: List[Dict[str, Any]] = []
        self.cooling_off_cases: Dict[str, Dict[str, Any]] = {}
        self.guardian_alerts: List[Dict[str, Any]] = []
        self.locked_subaccount_cents = 0
        self.subaccount_locks: Dict[str, Dict[str, Any]] = {}
        self.linkage_orders: List[Dict[str, Any]] = []
        self.family_members = {
            "妈妈": {"user_id": "family-mom-demo", "role": "被守护人", "allowance_cents": 200000, "authorized_actions": ["查看余额", "查看流水", "设置零花钱上限"]},
            "女儿": {"user_id": "family-child-demo", "role": "守护人", "allowance_cents": None, "authorized_actions": ["接收异常转账提醒", "设置零花钱上限"]},
        }
        # 演示授权矩阵：生产环境应来自实名关系、设备绑定、可撤销授权和统一策略中心。
        self.family_authorizations = [
            {"actor": "demo-user", "subject": "妈妈", "action": "set_allowance", "expires_at": "2099-12-31T23:59:59+00:00", "revoked": False, "cap_cents": 500000},
            {"actor": "demo-user", "subject": "妈妈", "action": "receive_fraud_alert", "expires_at": "2099-12-31T23:59:59+00:00", "revoked": False, "cap_cents": None},
        ]
        self._restore_core_transactions()
        self.db.execute("CREATE TABLE IF NOT EXISTS core_workflow_effects (effect_key TEXT PRIMARY KEY, result_json TEXT NOT NULL)")
        self.db.commit()
        self._restore_workflow_effects()

    def workflow_result(self, tool, task_id):
        with self.lock:
            row = self.db.execute('SELECT result_json FROM core_workflow_effects WHERE effect_key=?',
                                  (tool + ':' + task_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def _save_workflow_result(self, tool, task_id, data):
        # Durable core evidence BEFORE acknowledging the simulated effect. Production:
        # replace with core/merchant idempotent command + read-only status adapter.
        with self.db:
            self.db.execute('INSERT INTO core_workflow_effects VALUES (?,?)',
                            (tool + ':' + task_id, json.dumps(data, ensure_ascii=False)))

    def _restore_workflow_effects(self):
        self.balance_cents += self.locked_subaccount_cents
        self.subaccount_locks, self.linkage_orders = {}, []
        self.locked_subaccount_cents = 0
        rows = self.db.execute('SELECT effect_key,result_json FROM core_workflow_effects').fetchall()
        for key, raw in rows:
            data = json.loads(raw)
            if key.startswith('lock_subaccount:'):
                self.subaccount_locks[data['task_id']] = data
            elif key.startswith('order_'):
                self.linkage_orders.append(data)
        for key, raw in rows:
            if key.startswith('compensate_subaccount_lock:'):
                data = json.loads(raw)
                self.subaccount_locks[data['task_id']] = data
        self.locked_subaccount_cents = sum(x['amount_cents'] for x in self.subaccount_locks.values() if x['status'] == 'LOCKED')
        self.balance_cents -= self.locked_subaccount_cents

    def import_linkage_evidence(self, task):
        # Import proven historical results, never invoke an effect to reconstruct it.
        with self.lock:
            for node in task['nodes']:
                if node.get('result') and node['status'] in {'COMPLETED', 'COMPENSATED'}:
                    if self.workflow_result(node['action'], task['task_id']) is None:
                        self._save_workflow_result(node['action'], task['task_id'], dict(node['result'], task_id=task['task_id'], actor_id=task['actor_id'], **({'kind': 'flowers' if node['node_id'] == 'B' else 'cake'} if node['node_id'] in {'B', 'C'} else {})))
                if node.get('compensation') and self.workflow_result('compensate_subaccount_lock', task['task_id']) is None:
                    self._save_workflow_result('compensate_subaccount_lock', task['task_id'], dict(node['compensation'], task_id=task['task_id'], actor_id=task['actor_id']))
            self._restore_workflow_effects()

    def close(self) -> None:
        with self.lock:
            self.db.close()
            self.guardianships.close()

    def inject_fault(self, fault: Optional[str]) -> None:
        """设置离线演练故障；生产替换点是核心/支付网关的可控故障注入接口。"""
        with self.lock:
            self.fault_injection = fault

    def _restore_core_transactions(self) -> None:
        rows = self.db.execute("SELECT * FROM core_transactions ORDER BY created_at ASC").fetchall()
        debited = 0
        for row in rows:
            history = json.loads(row["state_history_json"])
            if row["debit_applied"]:
                if row["actor_id"] not in self.guardianships.accounts:
                    debited += row["amount_cents"]
                self.transactions.insert(0, {"id": row["transaction_id"], "date": row["created_at"][:10], "merchant": row["beneficiary"], "category": "转账", "amount_cents": -row["amount_cents"], "note": row["note"], "actor_id": row["actor_id"], "status": row["status"]})
        self.balance_cents -= debited

    def _row_for_transaction(self, transaction_id: str) -> Optional[sqlite3.Row]:
        return self.db.execute("SELECT * FROM core_transactions WHERE transaction_id = ?", (transaction_id,)).fetchone()

    def _row_for_idempotency(self, idempotency_key: str) -> Optional[sqlite3.Row]:
        row = self.db.execute("SELECT * FROM core_transactions WHERE idempotency_key = ?", (idempotency_key,)).fetchone()
        # Compatibility lookup only: canonical commands keep idem:{dag}:{node}.
        parts = idempotency_key.split(':')
        if row is None and len(parts) == 3 and parts[2] == 'create_transfer':
            if self.db.execute("SELECT 1 FROM sqlite_master WHERE name='task_dags'").fetchone():
                state = self.db.execute('SELECT state_json FROM task_dags WHERE dag_id=?', ('plan-' + parts[1],)).fetchone()
                if state:
                    for node in json.loads(state[0])['nodes']:
                        if node['tool'] == 'create_transfer':
                            return self.db.execute('SELECT * FROM core_transactions WHERE idempotency_key=?', (node['idempotency_key'],)).fetchone()
        return row

    def _transaction_data(self, row: sqlite3.Row, idempotent_replay: bool = False) -> Dict[str, Any]:
        data = {
            "transaction_id": row["transaction_id"],
            "status": row["status"],
            "amount_cents": row["amount_cents"],
            "beneficiary": row["beneficiary"],
            "note": row["note"],
            "actor_id": row["actor_id"],
            "state_history": json.loads(row["state_history_json"]),
        }
        if row["error_code"]:
            data["error_code"] = row["error_code"]
            data["error_message"] = row["error_message"]
        if idempotent_replay:
            data["idempotent_replay"] = True
        return data

    def _transition(self, transaction_id: str, status: str, error_code: Optional[str] = None, error_message: Optional[str] = None, debit_applied: Optional[bool] = None) -> sqlite3.Row:
        if status not in self.TRANSACTION_STATUSES:
            raise ValueError(f"未知交易状态: {status}")
        row = self._row_for_transaction(transaction_id)
        if row is None:
            raise ValueError(f"交易不存在: {transaction_id}")
        if status != row["status"] and status not in self.ALLOWED_TRANSITIONS[row["status"]]:
            raise ValueError(f"非法交易状态迁移: {row['status']} -> {status}")
        history = json.loads(row["state_history_json"])
        if status != row["status"]:
            history.append(status)
        applied = row["debit_applied"] if debit_applied is None else int(debit_applied)
        self.db.execute(
            "UPDATE core_transactions SET status=?, error_code=?, error_message=?, debit_applied=?, state_history_json=?, updated_at=? WHERE transaction_id=?",
            (status, error_code, error_message, applied, json.dumps(history, ensure_ascii=False), now_iso(), transaction_id),
        )
        self.db.commit()
        return self._row_for_transaction(transaction_id)

    def _create_transaction(self, amount_cents: int, name: str, note: str, idempotency_key: str, actor_id: str) -> sqlite3.Row:
        transaction_id = new_id("tx")
        now = now_iso()
        self.db.execute(
            "INSERT INTO core_transactions(transaction_id,idempotency_key,status,amount_cents,beneficiary,note,actor_id,error_code,error_message,debit_applied,state_history_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (transaction_id, idempotency_key, "created", amount_cents, name, note, actor_id, None, None, 0, json.dumps(["created"]), now, now),
        )
        self.db.commit()
        return self._row_for_transaction(transaction_id)

    def _result_for_row(self, row: sqlite3.Row, tool: str = "create_transfer", idempotent_replay: bool = False) -> ToolResult:
        data = self._transaction_data(row, idempotent_replay)
        if row["status"] == "failed":
            return ToolResult(False, tool, data, row["error_code"], row["error_message"])
        return ToolResult(True, tool, data)

    def query_transaction(self, transaction_id: str) -> ToolResult:
        """查询核心最终状态；只读，不会触发扣款或重试。

        演示故障注入只模拟“查询链路仍不可达”，不把 unknown 写入
        ``core_transactions``；核心账务事实仍保持 processing，待链路恢复
        后可以读到 processing/succeeded/failed。
        """
        with self.lock:
            row = self._row_for_transaction(transaction_id)
            if row is None:
                return ToolResult(False, "query_transaction", error_code="TRANSACTION_NOT_FOUND", error_message="未找到这笔交易。")
            key = row['idempotency_key']
            if key.startswith('idem:plan-'):
                key = 'idem:' + key.split(':')[1][5:] + ':create_transfer'
            if row["status"] == "processing" and self.fault_injection in {"transport_timeout", "network_unreachable"}:
                data = self._transaction_data(row)
                data.update({"status": "unknown", "outcome_unknown": True, "error_code": "OUTCOME_UNKNOWN",
                             "error_message": "银行暂时无法确认这笔交易。"})
                return ToolResult(True, "query_transaction", data, "OUTCOME_UNKNOWN", "银行暂时无法确认这笔交易。")
            self.guardianships.settle(key, row['status'])
            data = self._transaction_data(row)
            return ToolResult(True, "query_transaction", data, row["error_code"], row["error_message"])

    def get_transaction_status(self, transaction_id: str) -> ToolResult:
        return self.query_transaction(transaction_id)

    def finalize_transaction(self, transaction_id: str, outcome: str = "succeeded", error_code: Optional[str] = None, error_message: Optional[str] = None) -> ToolResult:
        with self.lock:
            row = self._row_for_transaction(transaction_id)
            if row is None:
                return ToolResult(False, "query_transaction", error_code="TRANSACTION_NOT_FOUND", error_message="未找到这笔交易。")
            if row["status"] != "processing":
                return self._result_for_row(row, tool="query_transaction", idempotent_replay=True)
            if outcome == "succeeded":
                if row["amount_cents"] > self.available_balance(row["actor_id"]):
                    outcome, error_code, error_message = "failed", "INSUFFICIENT_FUNDS", "可用余额不足。"
                else:
                    if row["actor_id"] not in self.guardianships.accounts:
                        self.balance_cents -= row["amount_cents"]
                    self.transactions.insert(0, {"id": row["transaction_id"], "date": today_key(), "merchant": row["beneficiary"], "category": "转账", "amount_cents": -row["amount_cents"], "note": row["note"], "actor_id": row["actor_id"], "status": "succeeded"})
                    row = self._transition(transaction_id, "succeeded", debit_applied=True)
                    self._roll_daily_counter()
                    return self._result_for_row(row, tool="query_transaction")
            if outcome == "failed":
                row = self._transition(transaction_id, "failed", error_code or "CORE_FAILED", error_message or "银行核心未完成这笔交易。")
                return self._result_for_row(row, tool="query_transaction")
            if outcome == "reversed":
                return self.reverse_transaction(transaction_id, error_message or "银行核心冲正")
            raise ValueError("finalize_transaction outcome 必须是 succeeded、failed 或 reversed")

    def reverse_transaction(self, transaction_id: str, reason: str = "银行核心冲正") -> ToolResult:
        with self.lock:
            row = self._row_for_transaction(transaction_id)
            if row is None:
                return ToolResult(False, "reverse_transaction", error_code="TRANSACTION_NOT_FOUND", error_message="未找到这笔交易。")
            if row["status"] == "reversed":
                return self._result_for_row(row, tool="reverse_transaction", idempotent_replay=True)
            if row["status"] != "succeeded":
                return ToolResult(False, "reverse_transaction", error_code="INVALID_STATE", error_message="只有成功交易可以冲正。")
            if row["debit_applied"]:
                if row["actor_id"] not in self.guardianships.accounts:
                    self.balance_cents += row["amount_cents"]
                self.transactions.insert(0, {"id": new_id("rv"), "date": today_key(), "merchant": row["beneficiary"], "category": "冲正", "amount_cents": row["amount_cents"], "reversal_of": row["transaction_id"], "actor_id": row["actor_id"]})
            row = self._transition(transaction_id, "reversed", error_code="REVERSED", error_message=reason, debit_applied=False)
            self._roll_daily_counter()
            return self._result_for_row(row, tool="reverse_transaction")

    def _roll_daily_counter(self) -> None:
        current = today_key()
        self.daily_transfer_date = current
        self.daily_transfer_cents = sum(
            abs(tx.get("amount_cents", 0))
            for tx in self.transactions
            if tx.get("date") == current
            and tx.get("actor_id", "demo-user") not in self.guardianships.accounts
            and tx.get("category") == "转账"
            and type(tx.get("amount_cents")) is int
            and tx.get("amount_cents", 0) < 0
        )

    def daily_transfer_total_cents(self, actor_id: str = "demo-user") -> int:
        """聚合当日已成交转账；交易流水是累计口径的唯一事实源。"""
        with self.lock:
            current = today_key()
            return sum(
                abs(tx["amount_cents"])
                for tx in self.transactions
                if tx.get("date") == current
                and tx.get("category") == "转账"
                and tx.get("actor_id", "demo-user") == actor_id
                and type(tx.get("amount_cents")) is int
                and tx["amount_cents"] < 0
            )

    def available_balance(self, actor_id="demo-user"):
        account = self.guardianships.accounts.get(actor_id)
        if account is None:
            return self.balance_cents
        debited = self.db.execute("SELECT coalesce(sum(amount_cents),0) FROM core_transactions WHERE actor_id=? AND debit_applied=1", (actor_id,)).fetchone()[0]
        return account['opening_cents'] - debited

    def balance(self, actor_id="demo-user") -> ToolResult:
        with self.lock:
            return ToolResult(True, "get_balance", {
                "available_cents": self.available_balance(actor_id), "currency": "CNY", "account_owner_id": actor_id,
                "coverage": {"complete": False, "scope": "mock_current_account_only",
                             "sources": [{"source": "MockBankCore", "owner_id": actor_id,
                                          "account_type": "current", "included": "available_balance_only"}],
                             "excluded": ["other_banks", "unimported_accounts", "investments", "locked_funds"]}})

    def confirmed_budget_commitments(self, actor_id: str = "demo-user") -> ToolResult:
        """只读返回已确认未来支出；预计收入不在此口径内。

        生产替换点：查询核心账务、信用卡账单和已授权扣款日历，并由服务端
        固化 confirmed 状态；本演示不把历史交易或预计工资当作承诺支出/余额。
        """
        with self.lock:
            items = [dict(item) for item in self.confirmed_commitments
                     if item.get("status") == "confirmed"
                     and type(item.get("amount_cents")) is int
                     and item.get("amount_cents", 0) >= 0]
            items.extend({
                "id": item["id"], "name": f"待付款订阅：{item.get('merchant', '未命名服务')}",
                "amount_cents": item["amount_cents"], "status": "confirmed",
            } for item in self.subscriptions
                if item.get("status") == "active"
                and type(item.get("amount_cents")) is int
                and item.get("amount_cents", 0) >= 0)
            return ToolResult(True, "confirmed_budget_commitments", {
                "items": items,
                "account_owner_id": actor_id,
                "projected_income_cents": 0,
            })

    def recent_transactions(self, actor_id="demo-user") -> ToolResult:
        with self.lock:
            items = [tx for tx in self.transactions if tx.get('actor_id', 'demo-user') == actor_id]
            return ToolResult(True, "list_transactions", {"items": items[:10], "account_owner_id": actor_id})

    def analyze_bills(self, actor_id="demo-user") -> ToolResult:
        with self.lock:
            items = [tx for tx in self.transactions if tx.get('actor_id', 'demo-user') == actor_id]
            by_cat = {}
            for tx in items:
                by_cat[tx["category"]] = by_cat.get(tx["category"], 0) + abs(tx["amount_cents"])
            return ToolResult(True, "analyze_bills", {"by_category": by_cat, "suspicious": [tx for tx in items if tx['category'] == '异常待核实'], "period": "近30天演示数据", "account_owner_id": actor_id})

    def resolve_beneficiary(self, name: str) -> ToolResult:
        with self.lock:
            candidates = self.beneficiary_candidates.get(name, [])
            if len(candidates) > 1:
                public_candidates = [
                    {
                        "beneficiary_id": candidate["beneficiary_id"],
                        "name": candidate["name"],
                        "phone_masked": candidate["phone"][:3] + "****" + candidate["phone"][-4:],
                    }
                    for candidate in candidates
                ]
                return ToolResult(
                    False,
                    "resolve_beneficiary",
                    {"name": name, "candidates": public_candidates, "requires_disambiguation": True},
                    error_code="BENEFICIARY_AMBIGUOUS",
                    error_message=f"找到{len(public_candidates)}位同名收款人，请补充手机号后四位。",
                )
            phone = self.beneficiaries.get(name)
            if not phone:
                return ToolResult(False, "resolve_beneficiary", error_code="BENEFICIARY_NOT_FOUND", error_message="未找到收款人，请补充姓名或手机号。")
            return ToolResult(True, "resolve_beneficiary", {"name": name, "phone_masked": phone[:3] + "****" + phone[-4:]})

    def check_transfer_constraints(self, amount_cents: int, actor_id: str = "demo-user") -> ToolResult:
        with self.lock:
            self._roll_daily_counter()
            if not strict_positive_int(amount_cents) or amount_cents > MAX_AMOUNT_CENTS:
                return ToolResult(False, "check_transfer_constraints", error_code="INVALID_AMOUNT", error_message="转账金额必须是正整数分，且不能超过演示上限。")
            daily_total = self.daily_transfer_total_cents(actor_id)
            projected = daily_total + amount_cents
            if amount_cents > self.available_balance(actor_id):
                return ToolResult(False, "check_transfer_constraints", error_code="INSUFFICIENT_FUNDS", error_message="可用余额不足。")
            if projected > POLICY["daily_transfer_limit_cents"]:
                return ToolResult(False, "check_transfer_constraints", error_code="DAILY_LIMIT", error_message="超过演示账户日累计转账上限。")
            return ToolResult(True, "check_transfer_constraints", {
                "available_cents": self.available_balance(actor_id),
                "daily_transfer_cents": daily_total,
                "projected_daily_transfer_cents": projected,
                "actor_id": actor_id,
                "allowed": True,
            })

    def transfer(self, amount_cents: int, name: str, note: str, idempotency_key: str, actor_id: str = "demo-user") -> ToolResult:
        with self.lock:
            existing = self._row_for_idempotency(idempotency_key)
            if existing is not None:
                return self._result_for_row(existing, idempotent_replay=True)
            if not strict_positive_int(amount_cents) or amount_cents > MAX_AMOUNT_CENTS:
                return ToolResult(False, "create_transfer", error_code="INVALID_AMOUNT", error_message="转账金额必须是正整数分，且不能超过演示上限。")
            row = self._create_transaction(amount_cents, name, note, idempotency_key, actor_id)
            tx_id = row["transaction_id"]
            # 冷静期到期后也不自动放行，必须人工核实；这是保护性控制，不是假装接入真实反诈平台。
            for case in self.cooling_off_cases.values():
                if case.get("beneficiary") != name or case.get("status") not in {"COOLING_OFF", "EXPIRED_PENDING_REVIEW"}:
                    continue
                try:
                    until = datetime.fromisoformat(case.get("cooling_off_until", ""))
                    if datetime.now(timezone.utc) < until and case.get("status") == "COOLING_OFF":
                        row = self._transition(tx_id, "processing")
                        row = self._transition(tx_id, "failed", "COOLING_OFF_ACTIVE", f"这位收款人正处于{self.fraud_rules['cooling_off_hours']}小时冷静期，系统不会继续转账，请先人工核实。")
                        return self._result_for_row(row)
                    case["status"] = "EXPIRED_PENDING_REVIEW"
                except (TypeError, ValueError):
                    case["status"] = "EXPIRED_PENDING_REVIEW"
                row = self._transition(tx_id, "processing")
                row = self._transition(tx_id, "failed", "COOLING_OFF_REVIEW_REQUIRED", "这位收款人曾触发反诈保护，即使冷静期结束也不能自动放行，请人工核实后再办理。")
                return self._result_for_row(row)
            self._roll_daily_counter()
            forced = self.fault_injection
            if forced == "beneficiary_not_found" or name not in self.beneficiaries:
                row = self._transition(tx_id, "processing")
                row = self._transition(tx_id, "failed", "BENEFICIARY_NOT_FOUND", "未找到收款人，请补充姓名或手机号。")
                return self._result_for_row(row)
            if forced == "insufficient_funds" or amount_cents > self.available_balance(actor_id):
                row = self._transition(tx_id, "processing")
                row = self._transition(tx_id, "failed", "INSUFFICIENT_FUNDS", "可用余额不足。")
                return self._result_for_row(row)
            if self.daily_transfer_total_cents(actor_id) + amount_cents > POLICY["daily_transfer_limit_cents"]:
                row = self._transition(tx_id, "processing")
                row = self._transition(tx_id, "failed", "DAILY_LIMIT", "超过演示账户日累计转账上限。")
                return self._result_for_row(row)
            row = self._transition(tx_id, "processing")
            if forced in {"timeout", "transport_timeout", "network_unreachable", "processing"}:
                # The durable C1 state remains processing. The caller only knows
                # that the transport outcome is uncertain; it must query this
                # idempotency key later and must never submit a second transfer.
                if forced in {"transport_timeout", "network_unreachable"}:
                    data = self._transaction_data(row)
                    data["status"] = "unknown"
                    data["outcome_unknown"] = True
                    data["error_code"] = "OUTCOME_UNKNOWN"
                    data["error_message"] = "银行暂时无法确认这笔交易。"
                    return ToolResult(True, "create_transfer", data)
                return self._result_for_row(row)
            if actor_id not in self.guardianships.accounts:
                self.balance_cents -= amount_cents
            self.transactions.insert(0, {"id": tx_id, "date": today_key(), "merchant": name, "category": "转账", "amount_cents": -amount_cents, "note": note, "actor_id": actor_id, "status": "succeeded"})
            row = self._transition(tx_id, "succeeded", debit_applied=True)
            self._roll_daily_counter()
            return self._result_for_row(row)

    def lock_subaccount(self, amount_cents: int, task_id: str, actor_id: str) -> ToolResult:
        with self.lock:
            existing = self.subaccount_locks.get(task_id)
            if existing and existing.get("status") == "LOCKED":
                return ToolResult(True, "lock_subaccount", dict(existing, idempotent_replay=True))
            if not strict_positive_int(amount_cents):
                return ToolResult(False, "lock_subaccount", error_code="INVALID_AMOUNT", error_message="锁定金额必须是正整数分。")
            if amount_cents > self.balance_cents:
                return ToolResult(False, "lock_subaccount", error_code="INSUFFICIENT_FUNDS", error_message="可用余额不足，无法锁定生日预算。")
            lock = {"task_id": task_id, "actor_id": actor_id, "amount_cents": amount_cents, "status": "LOCKED"}
            self._save_workflow_result('lock_subaccount', task_id, lock)
            self.balance_cents -= amount_cents
            self.locked_subaccount_cents += amount_cents
            self.subaccount_locks[task_id] = lock
            return ToolResult(True, "lock_subaccount", lock)

    def compensate_subaccount_lock(self, task_id: str) -> ToolResult:
        with self.lock:
            lock = self.subaccount_locks.get(task_id)
            if not lock:
                return ToolResult(False, "compensate_subaccount_lock", error_code="LOCK_NOT_FOUND", error_message="未找到可补偿的子账户锁定。")
            if lock.get("status") == "COMPENSATED":
                return ToolResult(True, "compensate_subaccount_lock", dict(lock, idempotent_replay=True))
            if lock.get("status") != "LOCKED":
                return ToolResult(False, "compensate_subaccount_lock", error_code="LOCK_NOT_COMPENSATABLE", error_message="该锁定当前不可补偿。")
            amount_cents = lock["amount_cents"]
            self._save_workflow_result('compensate_subaccount_lock', task_id, dict(lock, status='COMPENSATED'))
            self.balance_cents += amount_cents
            self.locked_subaccount_cents -= amount_cents
            lock["status"] = "COMPENSATED"
            return ToolResult(True, "compensate_subaccount_lock", dict(lock))

    def order_linkage_item(self, kind: str, task_id: str, actor_id: str) -> ToolResult:
        with self.lock:
            if kind not in {"flowers", "cake"}:
                return ToolResult(False, "order_linkage_item", error_code="INVALID_ORDER_KIND", error_message="不支持的生日礼物类型。")
            existing = next((item for item in self.linkage_orders if item["task_id"] == task_id and item["kind"] == kind), None)
            if existing:
                return ToolResult(True, "order_linkage_item", dict(existing, idempotent_replay=True))
            order = {"order_id": new_id("order"), "task_id": task_id, "actor_id": actor_id, "kind": kind, "status": "ORDERED"}
            self._save_workflow_result('order_' + kind, task_id, order)
            self.linkage_orders.append(order)
            return ToolResult(True, "order_linkage_item", order)

    @staticmethod
    def _subscription_layers(sub: Dict[str, Any]) -> Dict[str, Any]:
        """Return an isolated three-layer view; production replaces this with billing adapters."""
        return {
            "transaction": copy.deepcopy(sub.get("transaction_layer", {"history": []})),
            "authorization": copy.deepcopy(sub.get("authorization_layer", {})),
            "service": copy.deepcopy(sub.get("service_layer", {})),
        }

    def subscriptions_query(self) -> ToolResult:
        with self.lock:
            # Return deep copies so a read/query cannot mutate MockBankCore state.
            return ToolResult(True, "list_subscriptions", {"items": copy.deepcopy(self.subscriptions)})

    def cancel_subscription(self, sub_id: str, idempotency_key: str) -> ToolResult:
        with self.lock:
            if idempotency_key in self.idempotency_results:
                original = self.idempotency_results[idempotency_key]
                data = copy.deepcopy(original.data); data["idempotent_replay"] = True
                return ToolResult(original.ok, original.tool, data, original.error_code, original.error_message)
            sub = next((item for item in self.subscriptions if item["id"] == sub_id), None)
            if sub is None:
                return ToolResult(False, "cancel_subscription", error_code="SUBSCRIPTION_NOT_FOUND", error_message="未找到该订阅。")
            authorization = sub.get('authorization_layer', {})
            if (sum(item.get('id') == sub_id for item in self.subscriptions) != 1 or
                    authorization.get('terminable') is not True or authorization.get('debit_permission') != 'active'):
                return ToolResult(False, 'cancel_subscription', error_code='SUBSCRIPTION_REVIEW_REQUIRED',
                                  error_message='当前订阅不满足取消条件，请先核实扣费状态和办理渠道；本次未取消。')
            # Transport uncertainty is not evidence of success or failure. The
            # durable subscription remains unchanged and no automatic retry occurs.
            if self.fault_injection in {"transport_timeout", "network_unreachable"}:
                layers = self._subscription_layers(sub)
                data = {
                    "subscription_id": sub_id, "merchant": sub["merchant"], "status": "unknown",
                    "outcome_unknown": True, "pending_action": "待处理", "layers": {
                        "debit_authorization": {"status": "unknown", "message": "未来扣费是否已停止待核验"},
                        "service": {"status": layers["service"].get("member_status", "unknown"), "valid_until": layers["service"].get("valid_until"), "message": "服务状态待核验"},
                        "refund": {"status": "unknown", "amount_cents": 0, "message": "退款状态需向服务商核验"},
                    },
                }
                return ToolResult(True, "cancel_subscription", data, "OUTCOME_UNKNOWN", "订阅服务暂时无法确认取消结果。")
            sub["status"] = "cancelled"  # legacy compatibility; layer facts are authoritative for the user receipt.
            authorization = sub.setdefault("authorization_layer", {})
            authorization["debit_permission"] = "stopped"
            service = sub.setdefault("service_layer", {})
            service.setdefault("member_status", "active")
            service.setdefault("valid_until", sub.get("next_charge"))
            service.setdefault("exit_conditions", "停止代扣不等于立即终止服务")
            service.setdefault("refund_status", "unknown")
            service.setdefault("refund_amount_cents", 0)
            result = ToolResult(True, "cancel_subscription", {
                "subscription_id": sub_id, "merchant": sub["merchant"], "status": "cancelled",
                "layers": {
                    "debit_authorization": {"status": "stopped", "message": "未来扣费已停止"},
                    "service": {"status": service["member_status"], "valid_until": service.get("valid_until"), "message": "服务仍有效"},
                    "refund": {"status": service["refund_status"], "amount_cents": service["refund_amount_cents"], "message": "退款状态需向服务商核验"},
                },
            })
            self.idempotency_results[idempotency_key] = result
            return result

    def detect_fraud_signals(self, beneficiary: str, amount_cents: int, signals: List[str], is_new_recipient: bool) -> ToolResult:
        return ToolResult(True, "detect_fraud_signals", {
            "detected": bool(signals),
            "signals": list(signals),
            "beneficiary": beneficiary,
            "amount_cents": amount_cents,
            "is_new_recipient": is_new_recipient,
        })

    def create_cooling_off(self, beneficiary: str, amount_cents: int, signals: List[str], idempotency_key: str) -> ToolResult:
        with self.lock:
            if idempotency_key in self.idempotency_results:
                original = self.idempotency_results[idempotency_key]
                data = dict(original.data); data["idempotent_replay"] = True
                return ToolResult(original.ok, original.tool, data, original.error_code, original.error_message)
            cooling_off_hours = self.fraud_rules["cooling_off_hours"]
            until = datetime.now(timezone.utc) + timedelta(hours=cooling_off_hours)
            case_id = new_id("fraud")
            data = {
                "case_id": case_id,
                "beneficiary": beneficiary,
                "amount_cents": amount_cents,
                "signals": list(signals),
                "cooling_off_hours": cooling_off_hours,
                "cooling_off_until": until.isoformat(timespec="minutes"),
                "status": "COOLING_OFF",
            }
            self.cooling_off_cases[case_id] = data
            result = ToolResult(True, "create_cooling_off", data)
            self.idempotency_results[idempotency_key] = result
            return result

    def guardian_targets(self, actor):
        if actor in self.guardianships.accounts:
            return self.guardianships.guardian_targets(actor)
        target = self.guardian_target(actor)
        return [target] if target else []

    def guardian_target(self, actor: str) -> Optional[str]:
        if actor in self.guardianships.accounts:
            targets = self.guardianships.guardian_targets(actor)
            return targets[0] if targets else None
        for auth in self.family_authorizations:
            if auth["actor"] == actor and auth["subject"] == "妈妈" and auth["action"] == "receive_fraud_alert" and not auth["revoked"]:
                try:
                    if datetime.fromisoformat(auth["expires_at"]) > datetime.now(timezone.utc):
                        return self.family_members["女儿"]["user_id"]
                except (TypeError, ValueError):
                    pass
        return None

    def notify_guardian(self, case_id: str, beneficiary: str, amount_cents: int, idempotency_key: str, actor: str = "demo-user", notification: Optional[Dict[str, Any]] = None) -> ToolResult:
        with self.lock:
            if idempotency_key in self.idempotency_results:
                original = self.idempotency_results[idempotency_key]
                return ToolResult(original.ok, original.tool, dict(original.data, idempotent_replay=True), original.error_code, original.error_message)
            if (not self.guardian_target(actor) or not notification or notification.get("status") != "LOCAL_QUEUED"
                    or notification.get("user_id") != actor or notification.get("channel") != "guardian"
                    or notification.get("event_type") != "fraud.blocked" or notification.get("target") != self.guardian_target(actor)):
                return ToolResult(False, "notify_guardian", error_code="GUARDIAN_AUTH_MISSING", error_message="没有有效守护授权或可核验的通知总线回执，未发送家庭提醒。")
            # 兼容旧工具视图，事实源改为已审计的总线回执，不再维护另一条发送队列。
            alert = {"alert_id": notification["notification_id"], "event_id": notification["event_id"],
                     "case_id": case_id, "guardian": "女儿", "beneficiary": beneficiary,
                     "amount_cents": amount_cents, "status": notification["status"], "channel": "guardian",
                     "delivery_note": "未连接真实通知平台"}
            self.guardian_alerts.append(alert)
            result = ToolResult(True, "notify_guardian", alert)
            self.idempotency_results[idempotency_key] = result
            return result

    def financial_health_report(self, actor_id: str = "demo-user") -> ToolResult:
        with self.lock:
            from ledger_views import build_ledger_views

            items = [dict(tx) for tx in self.transactions if tx.get("actor_id", "demo-user") == actor_id]
            # The demo data is one account only.  Do not present it as a complete
            # household balance sheet; production replaces this with an
            # authorized multi-account ledger aggregation service.
            views = build_ledger_views(
                items,
                balances={"available_cents": self.available_balance(actor_id)},
                coverage={"scope": "演示账户", "complete": False, "updated_at": now_iso()},
            )
            category_totals = dict(views["consumer_view"]["by_category"])
            active_subscriptions = [x for x in self.subscriptions if x.get("status") == "active"]
            subscription_total = sum(x["amount_cents"] for x in active_subscriptions)
            suspicious = [x for x in items if x.get("category") == "异常待核实"]
            top_category = max(category_totals, key=category_totals.get) if category_totals else "暂无"
            summary = (
                f"近30天实际消费 {yuan(views['consumer_view']['total_cents'])}；"
                f"这张卡资金流出 {yuan(views['cash_flow_view']['outflow_cents'])}（包含还款，不等于消费）；"
                f"覆盖范围为{views['coverage']['scope']}。"
            )
            data = {
                "category_totals": category_totals,
                "subscription_total_cents": subscription_total,
                "suspicious_count": len(suspicious),
                "top_category": top_category,
                "summary": summary,
                "suggestions": ["核实陌生境外商户", "检查不常用的自动扣费", "为旅行或装修建立专门攒钱目标"],
                "ledger_views": views,
                "consumer_view": views["consumer_view"],
                "cash_flow_view": views["cash_flow_view"],
                "funds_status": views["funds_status"],
                "coverage": views["coverage"],
            }
            return ToolResult(True, "financial_health_report", data)

    def list_savings_goals(self) -> ToolResult:
        with self.lock:
            return ToolResult(True, "list_savings_goals", {"items": [dict(x) for x in self.savings_goals]})

    def create_savings_goal(self, goal_name: str, target_cents: int, monthly_cents: Optional[int], idempotency_key: str) -> ToolResult:
        with self.lock:
            if idempotency_key in self.idempotency_results:
                original = self.idempotency_results[idempotency_key]
                data = dict(original.data); data["idempotent_replay"] = True
                return ToolResult(original.ok, original.tool, data, original.error_code, original.error_message)
            if not strict_positive_int(target_cents) or target_cents > MAX_AMOUNT_CENTS:
                return ToolResult(False, "create_savings_goal", error_code="INVALID_GOAL", error_message="攒钱目标金额必须是正整数。")
            if monthly_cents is not None and (not strict_positive_int(monthly_cents) or monthly_cents > MAX_AMOUNT_CENTS):
                return ToolResult(False, "create_savings_goal", error_code="INVALID_MONTHLY_AMOUNT", error_message="每月攒钱金额必须是正整数。")
            goal = {"goal_id": new_id("goal"), "goal_name": goal_name or "攒钱目标", "target_cents": target_cents, "monthly_cents": monthly_cents, "saved_cents": 0, "status": "PLANNED"}
            self.savings_goals.append(goal)
            result = ToolResult(True, "create_savings_goal", goal)
            self.idempotency_results[idempotency_key] = result
            return result

    def idle_cash_check(self) -> ToolResult:
        with self.lock:
            threshold = POLICY["idle_cash_threshold_cents"]
            idle = max(0, self.balance_cents - threshold)
            message = f"发现约 {yuan(idle)} 暂时没有安排。你可以先设攒钱目标或咨询人工，不会自动替你购买理财。" if idle else "目前没有超过演示阈值的闲钱，不需要立即处理。"
            return ToolResult(True, "idle_cash_check", {"threshold_cents": threshold, "idle_cents": idle, "message": message})

    def list_family_relationships(self) -> ToolResult:
        with self.lock:
            return ToolResult(True, "list_family_relationships", {"members": {k: dict(v) for k, v in self.family_members.items()}, "summary": "家庭共管已开启：女儿可以接收妈妈的异常转账提醒，并在授权范围内设置零花钱上限。"})

    def family_auth_check(self, member: str, action: str, actor: str = "demo-user", allowance_cents: Optional[int] = None) -> ToolResult:
        with self.lock:
            if member not in self.family_members:
                return ToolResult(False, "family_auth_check", error_code="FAMILY_MEMBER_NOT_FOUND", error_message="没有找到这位家庭成员，请先建立家庭关系。")
            now = datetime.now(timezone.utc)
            matches = []
            for grant in self.family_authorizations:
                if grant["actor"] != actor or grant["subject"] != member or grant["action"] != action or grant["revoked"]:
                    continue
                try:
                    valid = now < datetime.fromisoformat(grant["expires_at"])
                except (TypeError, ValueError):
                    valid = False
                if valid and (allowance_cents is None or grant["cap_cents"] is None or allowance_cents <= grant["cap_cents"]):
                    matches.append(grant)
            if not matches:
                return ToolResult(False, "family_auth_check", error_code="FAMILY_SCOPE_DENIED", error_message="当前家庭授权已撤销、过期，或超过了可设置的限额。")
            return ToolResult(True, "family_auth_check", {"authorized": True, "actor": actor, "member": member, "action": action, "cap_cents": matches[0]["cap_cents"]})

    def set_family_allowance(self, member: str, allowance_cents: int, idempotency_key: str, actor: str = "demo-user") -> ToolResult:
        with self.lock:
            if idempotency_key in self.idempotency_results:
                original = self.idempotency_results[idempotency_key]
                data = dict(original.data); data["idempotent_replay"] = True
                return ToolResult(original.ok, original.tool, data, original.error_code, original.error_message)
            if not member in self.family_members:
                return ToolResult(False, "set_family_allowance", error_code="FAMILY_MEMBER_NOT_FOUND", error_message="没有找到这位家庭成员。")
            if not strict_positive_int(allowance_cents):
                return ToolResult(False, "set_family_allowance", error_code="INVALID_ALLOWANCE", error_message="零花钱上限必须是正整数。")
            # 在真正修改状态的同一把锁内再次核验，防止先核验后撤销授权的 TOCTOU。
            auth = self.family_auth_check(member, "set_allowance", actor, allowance_cents)
            if not auth.ok:
                return ToolResult(False, "set_family_allowance", error_code=auth.error_code, error_message=auth.error_message)
            self.family_members[member]["allowance_cents"] = allowance_cents
            result = ToolResult(True, "set_family_allowance", {"member": member, "allowance_cents": allowance_cents, "status": "UPDATED", "actor": actor})
            self.idempotency_results[idempotency_key] = result
            return result

    def calculate_aa_settlement(self, participants: List[str]) -> ToolResult:
        # 演示数据：只计算，不自动收款。
        participants = participants or ["妈妈", "女儿"]
        total_cents = 30000
        each = total_cents // len(participants)
        return ToolResult(True, "calculate_aa_settlement", {"participants": participants, "total_cents": total_cents, "each_cents": each, "summary": f"本次家庭共同支出 {yuan(total_cents)}，{len(participants)} 人平均每人 {yuan(each)}；双方确认后再办理。"})

    def investments_query(self) -> ToolResult:
        with self.lock:
            return ToolResult(True, "list_investments", {"items": list(self.investments)})

    def cards_query(self) -> ToolResult:
        with self.lock:
            # 对外查询使用既有中文状态，内部卡管 Adapter 保留结构化状态值。
            items = []
            for card in self.cards:
                item = dict(card)
                if item.get("status") == "reported_loss":
                    item["status"] = "挂失"
                items.append(item)
            return ToolResult(True, "list_cards", {"items": items})

    @staticmethod
    def _card_result(result: Any, tool: str) -> ToolResult:
        return ToolResult(
            result.ok, tool, dict(result.data),
            error_code=None if result.ok else result.code,
            error_message=None if result.ok else result.message,
            trace_id=result.trace_id,
        )

    def report_card_loss(self, card_id: str = "card_001", actor_id: str = "demo-user",
                         idempotency_key: str = "", trace_id: str = "") -> ToolResult:
        with self.lock:
            result = self.card_adapter.report_loss(
                AdapterContext(trace_id=trace_id, actor_id=actor_id, idempotency_key=idempotency_key), card_id)
            return self._card_result(result, "report_card_loss")

    def unreport_card_loss(self, card_id: str = "card_001", actor_id: str = "demo-user",
                           idempotency_key: str = "", trace_id: str = "") -> ToolResult:
        with self.lock:
            result = self.card_adapter.unreport_loss(
                AdapterContext(trace_id=trace_id, actor_id=actor_id, idempotency_key=idempotency_key), card_id)
            return self._card_result(result, "unreport_card_loss")

    def card_status_query(self, card_id: str = "card_001", actor_id: str = "demo-user",
                          trace_id: str = "") -> ToolResult:
        with self.lock:
            result = self.card_adapter.query_card(
                AdapterContext(trace_id=trace_id, actor_id=actor_id), card_id)
            return self._card_result(result, "verify_card_status")


# --------------------------- intent + policy layer ------------------------
class IntentParser:
    """将自然语言转为受限意图；金额先在这里做精确校验，不能把非法金额送进执行链。"""

    def __init__(self, fraud_rules: Optional[Dict[str, Any]] = None):
        self.fraud_rules = fraud_rules or load_fraud_rules()
        self.llm = LLMIntentAdapter()

    @staticmethod
    def _amount(raw: Optional[str]) -> Tuple[Optional[int], Optional[str]]:
        if raw is None:
            return None, None
        normalized = str(raw).replace(',', '').replace('，', '').strip()
        # 不接受科学计数法、空值和多余符号；宁可澄清，不猜金额。
        if not re.fullmatch(r"[+\-]?\d+(?:\.\d+)?", normalized):
            return None, "金额格式无法识别，请按“元”和“分”填写。"
        try:
            value = Decimal(normalized)
        except (InvalidOperation, ValueError):
            return None, "金额格式无法识别。"
        if not value.is_finite():
            return None, "金额必须是有限数字。"
        if value <= 0:
            return None, "转账金额必须大于0。"
        if value.as_tuple().exponent < -2:
            return None, "金额最多支持到分，不能使用千分之一元。"
        cents_decimal = value * Decimal("100")
        if cents_decimal != cents_decimal.to_integral_value():
            return None, "金额必须精确到人民币分。"
        cents = int(cents_decimal)
        if cents > MAX_AMOUNT_CENTS:
            return None, "金额过大，已停止办理并转人工核实。"
        return cents, None

    @staticmethod
    def _extract_amount(text: str) -> Tuple[Optional[int], Optional[str]]:
        # Notes may contain unrelated amounts. They never override transfer instructions.
        text = re.split(r"备注|说明", text, maxsplit=1)[0]
        # Explicit money units take priority; never interpret an account/phone as money.
        match = re.search(r"(?<![\d.])([+\-]?\d[\d,]*(?:\.\d+)?)\s*(?:元|块|人民币)", text)
        if match is None:
            without_accounts = re.sub(r"(?:账号|账户|卡号|手机号)\s*[:：]?\s*\d+", "", text)
            match = re.search(r"(?<![\d.])([+\-]?\d[\d,]*(?:\.\d+)?)", without_accounts)
        return IntentParser._amount(match.group(1) if match else None)

    @staticmethod
    def _extract_beneficiary(text: str) -> Optional[str]:
        # “安全账户”是反诈演示中的虚拟收款人，不能因为不存在于通讯录就继续执行。
        if "安全账户" in text:
            return "安全账户"
        if "这个账户" in text or "这个账号" in text:
            return "未知收款账户"
        # Bind the recipient to the transfer clause, not a familiar name anywhere in a note.
        clause = re.split(r"[，,。；;]|备注|说明", text, maxsplit=1)[0]
        match = re.search(r"(?:转给|汇给|给|向|转|汇)\s*(?:我(?:的)?)?(?P<name>[\u4e00-\u9fa5A-Za-z]{1,12}?)(?=\s*(?:转账|转|汇款|汇)?\s*[+\-]?\d|转账?$|汇款?$)", clause)
        if not match:
            return None
        name = match.group("name")
        # Account-kind wording is not part of the recipient's name. Explicit
        # account numbers still require verification in the transfer parser.
        name = re.sub(r'(?:的)?银行卡$', '', name)
        return None if not name or name in {'账', '款'} or any(word in name for word in ("账号", "账户", "卡号", "金额", "账到", "设置", "上限", "限额")) else name

    @staticmethod
    def _birthday_date(text: str) -> str:
        current = date.fromisoformat(today_key())
        explicit = re.search(r"(?<!\d)(\d{4})[年/.-](\d{1,2})[月/.-](\d{1,2})日?(?!\d)", text)
        if explicit:
            try:
                return date(*map(int, explicit.groups())).isoformat()
            except ValueError:
                return ""
        match = re.search(r"(?<!\d)(\d{1,2})\s*[月/.-]\s*(\d{1,2})\s*(?:日)?(?!\d)", text)
        if match:
            month, day = int(match.group(1)), int(match.group(2))
            for year in range(current.year, current.year + 9):
                try:
                    candidate = date(year, month, day)
                except ValueError:
                    continue
                if candidate >= current:
                    return candidate.isoformat()
        return ""

    @staticmethod
    def birthday_schedule_error(birthday_date, flowers_days_before, cake_days_before):
        if any(type(v) is not int or not 0 <= v <= 365 for v in (flowers_days_before, cake_days_before)):
            return '请分别指定鲜花和蛋糕提前0至365个整数天数；未创建任务或扣款。'
        try:
            birthday = date.fromisoformat(birthday_date)
        except (TypeError, ValueError):
            return '请补充有效的生日日期；未创建任务或扣款。'
        current = date.fromisoformat(today_key())
        if birthday < current or any(birthday - timedelta(days=v) < current for v in (flowers_days_before, cake_days_before)):
            return '生日或订购日期已经过去，请重新指定日期和提前天数；未创建任务或扣款。'
        return ''

    @staticmethod
    def _lead_days(raw):
        if raw.isascii() and raw.isdigit():
            return int(raw)
        digits = dict(zip('零〇一二两三四五六七八九', (0, 0, 1, 2, 2, 3, 4, 5, 6, 7, 8, 9)))
        # Deliberately finite grammar: reject fractions, weeks and unsupported wording.
        if raw in digits:
            return digits[raw]
        if re.fullmatch(r'[一二两三]?百(?:零?[一二两三四五六七八九])?(?:十[一二三四五六七八九]?)?|[一二两三四五六七八九]?十[一二三四五六七八九]?', raw):
            total, pending = 0, 0
            for char in raw:
                if char in digits:
                    pending = digits[char]
                else:
                    total += (pending or 1) * {'十': 10, '百': 100}[char]
                    pending = 0
            return total + pending
        return None

    @classmethod
    def _birthday_entities(cls, text: str) -> Dict[str, Any]:
        entities = {
            "recipient": "爱人",
            "birthday_date": cls._birthday_date(text),
            "lock_amount_cents": POLICY["birthday_lock_amount_cents"],
            "flowers_days_before": 2, "cake_days_before": 1,
        }
        item = r'(?:鲜花|订花|花|蛋糕)'
        number = r'[+\-]?\d+(?:\.\d+)?|[零〇一二两三四五六七八九十百]+'
        patterns = [rf'(?P<item>{item})(?:在)?(?:生日)?(?:提前|前)\s*(?P<n>{number})\s*天',
                    rf'(?:生日)?提前\s*(?P<n>{number})\s*天(?:订购|购买|订|买)?(?P<item>{item})',
                    rf'(?P<item>{item})(?:生日)?(?P<same>当天|当日)']
        seen, remaining = {}, text
        for pattern in patterns:
            for match in list(re.finditer(pattern, remaining)):
                key = 'cake_days_before' if match['item'] == '蛋糕' else 'flowers_days_before'
                days = 0 if match.groupdict().get('same') else cls._lead_days(match['n'])
                if days is None or (key in seen and seen[key] != days):
                    entities['schedule_error'] = '提前天数无效或相互矛盾，请分别明确鲜花和蛋糕的整数天数。'
                seen[key] = days
            remaining = re.sub(pattern, '', remaining)
        if re.search(r'提前|当天|当日|天前|天后|延后|之后|生日前', remaining):
            entities['schedule_error'] = '订购时间未能明确对应鲜花或蛋糕，请分别说明，例如鲜花提前3天、蛋糕提前1天。'
        entities.update(seen)
        entities['schedule_error'] = entities.get('schedule_error') or cls.birthday_schedule_error(
            entities['birthday_date'], entities['flowers_days_before'], entities['cake_days_before'])
        return entities

    def _fraud_signals(self, text: str, beneficiary: Optional[str], amount_cents: Optional[int]) -> List[str]:
        lowered = text.casefold()
        signals = [
            rule["reason"]
            for rule in self.fraud_rules["keyword_rules"]
            if rule["keyword"].casefold() in lowered
        ]
        # New/unknown recipients and amounts are risk factors, not scam evidence.
        # PolicyEngine still scores them and can defer or demand authentication.
        if beneficiary == "安全账户":
            signals.append("收款账户无法核验")
        return list(dict.fromkeys(signals))

    def parse(self, text: str) -> Tuple[str, Dict[str, Any]]:
        self.llm.reset_evidence("local_priority")
        t = text.strip()
        # Explicit explanation mode only; no model can route a bank action into RAG.
        knowledge = re.match(r'^(?:知识问答|银行知识)\s*[:：]\s*(.*)$', t, flags=re.S)
        if knowledge:
            question = knowledge.group(1).strip()
            rejected = ('privacy_blocked' if KnowledgeService.sensitive_input(question) else
                        'invalid_input' if not 0 < len(question) <= 400 else '')
            return 'knowledge_query', {'question': '' if rejected else question, 'knowledge_rejection': rejected}
        # Display requests (including negated loss wording) cannot become card writes.
        if '卡' in t and any(word in t for word in ('不显示', '隐藏', '恢复显示')):
            return 'unknown', {'clarification': 'card_display_only'}
        if '密码' in t and any(word in t for word in ('修改', '重置', '更改')):
            return 'password_change', {}
        # Negation and status questions are read requests, before any model or keyword routing.
        negative = r'(?:不要|不用|不想|不需要|先别|别|暂不|暂时不|不(?=挂失|解挂|取消|关闭|清理|办理|进行))[^，,。；;！？!?]*(?:挂失|解挂|取消|关闭|清理)'
        status_question = r'(?:挂失|解挂|取消|关闭)[^，,。；;！？!?]*(?:了吗|了没有|了没|是否成功|成功了吗)'
        read_only = bool(re.search(negative, t) or re.search(status_question, t))
        if (read_only or re.search(r'(?:取消|撤销)[^，,。；;]*(?:挂失|解挂)(?:申请|操作)', t)) and any(w in t for w in ('卡', '挂失', '解挂')):
            return 'card_manage', {}
        if read_only and any(w in t for w in ('订阅', '会员', '自动扣费', '自动续费', '代扣')):
            return 'subscription_query', {}
        card_match = re.fullmatch(r'(挂失|解挂)卡片编号(card_[A-Za-z0-9_-]+)', t)
        if card_match:
            return 'card_manage', {'card_operation':'report_loss' if card_match[1]=='挂失' else 'unreport_loss', 'card_id':card_match[2]}
        subscription_match = re.fullmatch(r'取消订阅编号(sub_[A-Za-z0-9_-]+)', t)
        if subscription_match:
            return 'subscription_cancel', {'suggested_subscription_id':subscription_match[1]}
        if any(word in t for word in ('解挂', '解除挂失', '取消挂失')):
            return 'card_manage', {'card_operation': 'unreport_loss'}
        if any(word in t for word in ('挂失', '卡丢了', '卡丢失', '卡不见了')):
            return 'card_manage', {'card_operation': 'report_loss'}
        # 安全前置：转账相关文本先由本地规则解析，LLM 不能覆盖金额、收款人和反诈判定。
        local_beneficiary = self._extract_beneficiary(t)
        local_amount, local_amount_error = self._extract_amount(t)
        transfer_like = bool(local_beneficiary) or any(k in t for k in ["转账", "转给", "转", "汇款", "汇给", "打款"])
        local_fraud = self._fraud_signals(t, local_beneficiary, local_amount)
        if "生日" in t and any(word in t for word in ["爱人", "老婆", "老公", "伴侣", "对象"]):
            return "birthday_linkage", self._birthday_entities(t)
        if transfer_like:
            note_match = re.search(r"(?:备注|说明)[：:]?\s*([^，,。]+)", t)
            note = note_match.group(1).strip() if note_match else ""
            entities = {
                "beneficiary": local_beneficiary,
                "amount_cents": local_amount,
                "amount_error": local_amount_error,
                # Do not store raw account numbers or silently choose the name's old account.
                "account_requires_verification": bool(re.search(r"(?:账号|账户|卡号|银行卡|手机号)\s*[:：]?\s*\d", t)),
                "note": note,
                "fraud_signals": local_fraud,
                "fraud_keyword_hit": any(rule["keyword"].casefold() in t.casefold() for rule in self.fraud_rules["keyword_rules"]),
                "is_new_recipient": bool(local_beneficiary) and local_beneficiary not in {"妈妈", "李明", "张伟"},
            }
            if local_fraud:
                entities["fraud_hotline"] = self.fraud_rules["hotline"]
                entities["cooling_off_hours"] = self.fraud_rules["cooling_off_hours"]
                return "fraud_guard", entities
            return "transfer", entities

        if '卡片管理' in t or '卡片状态' in t or '银行卡' in t:
            return 'card_manage', {}
        # 非资金副作用意图可以使用可选模型；其输出仍只进入受限实体协议。
        llm_result = self.llm.parse(text)
        if llm_result is not None:
            intent, entities = llm_result
            if intent == "subscription_cancel":
                entities = dict(entities)
                hint = entities.pop("subscription_id", None)
                if not hint:
                    hint = "sub_001" if "视频" in text and "云盘" not in text else "sub_002" if "云盘" in text and "视频" not in text else None
                entities["suggested_subscription_id"] = hint
            return intent, entities

        if any(k in t for k in ["反诈", "防诈骗", "诈骗提醒"]):
            return "fraud_guard", {"fraud_signals": ["用户主动请求反诈守护"]}
        if any(k in t for k in ["消费体检", "财务健康", "消费报告", "消费诊断", "这个月我到底花了多少钱", "我这个月花了多少", "这张卡这个月流出了多少", "下周要准备多少钱", "整体财务状况"]):
            return "financial_health", {"action": "report"}
        if any(k in t for k in ["攒钱目标", "旅行基金", "装修基金", "自动攒钱", "存钱目标"]):
            name = "旅行基金" if "旅行" in t else "装修基金" if "装修" in t else "攒钱目标"
            target_match = re.search(r"目标(?:金额)?\s*([+\-]?\d[\d,]*(?:\.\d+)?)\s*元", t)
            target, target_error = self._amount(target_match.group(1)) if target_match else (None, None)
            monthly_match = re.search(r"每月(?:攒|存)\s*([+\-]?\d[\d,]*(?:\.\d+)?)\s*元", t)
            monthly, monthly_error = self._amount(monthly_match.group(1)) if monthly_match else (None, None)
            return "financial_health", {"action": "savings_goal", "goal_name": name, "target_cents": target, "monthly_cents": monthly, "amount_error": target_error or monthly_error}
        if any(k in t for k in ["闲钱提醒", "闲钱打理", "闲置资金", "闲钱"]):
            return "financial_health", {"action": "idle_cash"}
        if any(k in t for k in ["家庭共管", "家庭成员", "亲情守护", "家人授权", "家庭关系"]):
            return "family_co_management", {"action": "overview"}
        if any(k in t for k in ["AA收款", "AA", "家庭分摊", "家庭AA", "平均分摊"]):
            participants = [n for n in ["妈妈", "女儿", "爸爸", "儿子"] if n in t]
            return "family_co_management", {"action": "aa_settlement", "participants": participants}
        allowance_match = re.search(r"(?:给|为)(?P<member>妈妈|爸爸|父母|女儿|儿子)(?:设置|设定|调整)?(?:每月)?(?:零花钱|生活费)?(?:上限|限额)\s*([+\-]?\d[\d,]*(?:\.\d+)?)\s*元", t)
        if allowance_match or any(k in t for k in ["零花钱上限", "生活费限额", "给父母设置限额"]):
            member = allowance_match.group("member") if allowance_match else ("妈妈" if "妈妈" in t or "父母" in t else "家人")
            raw = allowance_match.group(2) if allowance_match else None
            allowance, amount_error = self._amount(raw)
            return "family_co_management", {"action": "set_allowance", "member": member, "allowance_cents": allowance, "amount_error": amount_error}
        if any(k in t for k in ["余额", "账户余额", "有多少钱"]):
            return "balance_query", {}
        if any(k in t for k in ['全部资产', '所有资产', '总资产']):
            return 'balance_query', {'requested_scope': 'all_assets'}
        if any(k in t for k in ["账单分析", "消费分析", "花了多少钱", "消费分类"]):
            return "bill_analysis", {}
        if any(k in t for k in ["交易记录", "最近消费", "最近的消费", "流水"]):
            return "transaction_query", {}
        if any(k in t for k in ["订阅", "自动扣费", "代扣", "会员"]):
            if any(k in t for k in ["取消", "关闭", "清理"]):
                sid = "sub_001" if "视频" in t and "云盘" not in t else "sub_002" if "云盘" in t and "视频" not in t else None
                return "subscription_cancel", {"suggested_subscription_id": sid}
            return "subscription_query", {}
        if any(k in t for k in ["理财收益", "投资收益", "持仓"]):
            return "investment_query", {}
        if any(k in t for k in ["卡片状态", "我的卡", "银行卡"]):
            return "card_manage", {}
        if any(k in t for k in ["挂失", "解锁", "卡片管理"]):
            return "card_manage", {}
        return "unknown", {"raw": t}


class PolicyEngine:
    def __init__(self, bank: MockBankCore):
        self.bank = bank

    def classify(self, intent: str, entities: Dict[str, Any], actor_id: str = "demo-user") -> Dict[str, Any]:
        decision = self._base_classify(intent, entities, actor_id)
        decision.update(risk_score=0, risk_factors=[], aml_suspect=False, defer=False,
                        decision_reason="既有权限规则：" + decision["risk_level"])
        if intent == 'knowledge_query':
            context=entities.get('_family')
            reader,subject=actor_id,actor_id
            if context:
                store=self.bank.guardianships
                grant=store.get(context.get('grant_id',''))
                reader=context.get('guardian_id','')
                guardian=store.accounts.get(reader,{})
                valid=(grant is not None and grant.active(store.clock()) and grant.subject_id==subject and
                       context.get('subject_id')==subject and grant.guardian_id==reader and
                       guardian.get('device_id')==context.get('guardian_device_id') and
                       context.get('mode') in {'delegate','assist'})
                if not valid:
                    return dict(decision,authz_allowed=False,decision_reason='家庭资料阅读关系无效、过期或已撤销')
            return dict(decision,authz_allowed=True,knowledge_actor=reader,knowledge_subject=subject,
                        knowledge_delegated=bool(context),decision_reason='只读知识访问；私有资料仍须本人或明确家庭共享授权')
        if intent in {"transfer", "fraud_guard"} and strict_positive_int(entities.get("amount_cents")) and not entities.get("amount_error"):
            decision.update(assess_transfer_risk(self.bank, entities, actor_id))
            decision["defer"] = decision["risk_score"] >= POLICY["risk_defer_score"] or intent == "fraud_guard"
            if decision["defer"]:
                decision.update(risk_level="red", requires_confirmation=False, requires_strong_auth=False)
                decision["decision_reason"] += "；暂缓转账，等待人工核实" if intent == "transfer" else "；既有反诈硬规则拦截"
            elif decision["aml_suspect"]:
                decision.update(risk_level="red", requires_confirmation=True, requires_strong_auth=True)
                decision["decision_reason"] += "；升级红色，必须强认证"
            elif decision["requires_strong_auth"]:
                decision["decision_reason"] += "；日累计金额或既有规则要求强认证"
        if actor_id in self.bank.guardianships.accounts and intent not in {
                'balance_query', 'transaction_query', 'bill_analysis', 'transfer', 'fraud_guard', 'card_manage', 'password_change'}:
            decision.update(authz_allowed=False, decision_reason='该账户业务尚未接入隔离核心，拒绝执行')
        context = entities.get('_family')
        if context:
            matrix = self.bank.guardianships.evaluate(intent, entities.get('amount_cents'), context, actor_id)
            decision.update(authz_allowed=matrix['allowed'])
            decision['decision_reason'] += '；' + matrix['reason']
            if matrix['owner_auth_required'] and not decision.get('defer'):
                decision.update(risk_level='red', requires_confirmation=True, requires_strong_auth=True)
        return decision

    def classify_tool(self, intent, entities, actor_id, tool):
        decision = self.classify(intent, entities, actor_id)
        card_operations = {'report_card_loss':'report_loss', 'unreport_card_loss':'unreport_loss'}
        if tool in card_operations and (intent != 'card_manage' or not entities.get('card_id') or
                                        entities.get('card_operation') != card_operations[tool]):
            decision.update(authz_allowed=False, decision_reason='卡片写操作必须绑定明确的卡片和操作')
        if ((tool == 'knowledge_lookup' and intent != 'knowledge_query') or
                (intent == 'knowledge_query' and tool != 'knowledge_lookup')):
            decision.update(authz_allowed=False, decision_reason='知识问答只允许绑定的只读资料工具')
        if actor_id in self.bank.guardianships.accounts:
            tools = {
                'knowledge_query': {'knowledge_lookup'},
                'balance_query': {'get_balance'}, 'transaction_query': {'list_transactions'},
                'bill_analysis': {'list_transactions', 'analyze_bills'},
                'transfer': {'resolve_beneficiary', 'check_transfer_constraints', 'create_transfer'},
                'fraud_guard': {'detect_fraud_signals', 'create_cooling_off', 'notify_guardian'},
                'password_change': {'human_handoff'}, 'card_manage': {'human_handoff'},
            }
            if tool not in tools.get(intent, set()):
                decision.update(authz_allowed=False, decision_reason='工具不在该账户业务的授权范围内')
        return decision

    def _base_classify(self, intent: str, entities: Dict[str, Any], actor_id: str = "demo-user") -> Dict[str, Any]:
        # Only the platform policy can distinguish a read from a card write.
        # Family card handling retains its existing owner-auth/handoff boundary.
        if (intent == 'card_manage' and actor_id == 'demo-user' and not entities.get('_family') and
                (not entities.get('card_operation') or
                 (entities.get('card_operation') in {'report_loss', 'unreport_loss'} and not entities.get('card_id')))):
            return {"risk_level":"green", "requires_confirmation":False, "requires_strong_auth":False}
        if intent == 'knowledge_query':
            return {"risk_level": "green", "requires_confirmation": False, "requires_strong_auth": False}
        if intent in POLICY["green"]:
            if intent == "financial_health" and entities.get("action") == "savings_goal":
                return {"risk_level": "yellow", "requires_confirmation": True, "requires_strong_auth": False}
            return {"risk_level": "green", "requires_confirmation": False, "requires_strong_auth": False}
        if intent == "birthday_linkage":
            return {"risk_level": "yellow", "requires_confirmation": True, "requires_strong_auth": False}
        if intent == "fraud_guard":
            # 反诈是保护性阻断：不要求用户再次确认“是否继续转账”，避免社会工程绕过。
            return {"risk_level": "red", "requires_confirmation": False, "requires_strong_auth": False}
        if intent == "family_co_management":
            if entities.get("action") == "set_allowance":
                return {"risk_level": "yellow", "requires_confirmation": True, "requires_strong_auth": False}
            return {"risk_level": "green", "requires_confirmation": False, "requires_strong_auth": False}
        if intent in POLICY["yellow"]:
            if intent == "transfer":
                amount = entities.get("amount_cents")
                if not isinstance(amount, int) or amount <= 0 or entities.get("amount_error"):
                    return {"risk_level": "red", "requires_confirmation": False, "requires_strong_auth": False}
                with self.bank.lock:
                    self.bank._roll_daily_counter()
                    projected = self.bank.daily_transfer_total_cents(actor_id) + amount
                if projected > POLICY["small_transfer_limit_cents"]:
                    return {"risk_level": "red", "requires_confirmation": True, "requires_strong_auth": True}
            return {"risk_level": "yellow", "requires_confirmation": True, "requires_strong_auth": False}
        if intent in POLICY["red"]:
            return {"risk_level": "red", "requires_confirmation": True, "requires_strong_auth": True}
        return {"risk_level": "yellow", "requires_confirmation": intent != "unknown" and not entities.get("amount_error"), "requires_strong_auth": False}


class Planner:
    def __init__(self, parser: IntentParser, policy: PolicyEngine, bank: MockBankCore):
        self.parser, self.policy, self.bank = parser, policy, bank

    def build(self, text: str, actor_id: str = "demo-user", device_id: str = "demo-device", family_context=None, trace_id: Optional[str] = None) -> AgentPlan:
        self.parser.llm.reset_evidence("feature_demo")
        feature_request = FEATURE_REGISTRY.parse_request(text)
        intent, entities = (FEATURE_INTENT, feature_request) if feature_request is not None else self.parser.parse(text)
        entities = dict(entities)
        entities.pop('_family', None)  # model output can never grant family authority
        if intent == 'card_manage' and entities.get('card_operation'):
            selected = entities.get('card_id')
            tails = re.findall(r'尾号\s*([0-9]{4})(?![0-9])', text)
            if not selected and len(tails) == 1:
                tail_matches = [c for c in self.bank.cards if c.get('last4') == tails[0]]
                if len(tail_matches) == 1:
                    selected = tail_matches[0].get('id')
            # Only an unqualified request with exactly one trusted card can use that card.
            if not selected and len(self.bank.cards) == 1 and not re.search(r'第|尾号|卡号|编号|另一|那张|这张', text):
                selected = self.bank.cards[0].get('id')
            matches = [c for c in self.bank.cards if c.get('id') == selected] if selected else []
            entities['card_id'] = selected if len(matches) == 1 else None
            entities['card_name'] = card_label(matches[0]) if len(matches) == 1 else None
        if intent == "subscription_cancel":
            candidate = entities.get("suggested_subscription_id") or entities.get("subscription_id")
            sub = next((x for x in self.bank.subscriptions if x['id'] == candidate), None)
            # Reject model-supplied names/prices and bind the exact visible target to consent.
            entities = {"subscription_id": sub['id'] if sub else None,
                        "suggested_subscription_id": candidate,
                        "subscription_name": sub['merchant'] if sub else None,
                        "subscription_amount_cents": sub['amount_cents'] if sub else None}
        if intent == "transfer" and not entities.get("account_requires_verification") and entities.get("beneficiary") in self.bank.beneficiaries:
            phone = self.bank.beneficiaries[entities["beneficiary"]]
            entities["beneficiary_account"] = phone[:3] + "****" + phone[-4:]
        if family_context is not None:
            entities['_family'] = dict(family_context)
        policy = self.policy.classify(intent, entities, actor_id)
        trace_id, plan_id = trace_id or new_id("trace"), new_id("plan")
        steps: List[PlanStep] = []
        summary = "需要补充更多信息"
        if intent == FEATURE_INTENT:
            summary, tool_name = FEATURE_REGISTRY.describe_request(entities)
            steps = [PlanStep("s1", summary, tool_name)]
        elif intent == 'knowledge_query':
            summary, steps = '只读检索项目知识资料并给出出处，不办理银行业务', [PlanStep('s1','检索与核对资料','knowledge_lookup')]
        elif intent == "balance_query":
            summary, steps = "查询活期账户可用余额", [PlanStep("s1", "读取账户余额", "get_balance")]
        elif intent == "transaction_query":
            summary, steps = "读取最近交易记录", [PlanStep("s1", "读取交易流水", "list_transactions")]
        elif intent == "bill_analysis":
            summary, steps = "按消费类别汇总账单，并标记异常交易", [PlanStep("s1", "读取近30天流水", "list_transactions"), PlanStep("s2", "生成消费分类与异常摘要", "analyze_bills")]
        elif intent == "investment_query":
            summary, steps = "查询当前理财持仓与收益", [PlanStep("s1", "读取理财持仓", "list_investments")]
        elif intent == "subscription_query":
            summary, steps = "查询自动续费与订阅服务", [PlanStep("s1", "读取订阅列表", "list_subscriptions")]
        elif intent == "subscription_cancel":
            summary = (f"申请停止{entities['subscription_name']}的自动扣费（每期{money(entities['subscription_amount_cents'])}）；确认后核验办理条件"
                       if entities.get('subscription_id') else "请说明要取消哪个订阅：视频会员还是云盘会员")
            steps = [PlanStep("s1", "定位订阅", "list_subscriptions"), PlanStep("s2", "取消订阅", "cancel_subscription")]
        elif intent == "card_manage":
            operation = entities.get("card_operation")
            if actor_id in self.bank.guardianships.accounts:
                summary, steps = "本人强认证后转官方人工办理；演示系统不代办家庭成员的卡片写操作", [PlanStep("s1", "本人认证后人工办理", "human_handoff")]
            elif not operation or not entities.get('card_id'):
                summary, steps = '核对卡片状态；选择具体卡片后再创建办理计划', [PlanStep('s1','读取卡片状态','list_cards')]
            elif operation == "report_loss":
                summary = f"挂失{entities['card_name']}；先强验证，再核验挂失后的卡片状态"
                steps = [PlanStep("s1", "读取卡片状态", "list_cards"), PlanStep("s2", "挂失卡片", "report_card_loss"), PlanStep("s3", "核验挂失后的卡片状态", "verify_card_status")]
            elif operation == "unreport_loss":
                summary = f"解除{entities['card_name']}挂失；先强验证，再核验恢复后的卡片状态"
                steps = [PlanStep("s1", "读取卡片状态", "list_cards"), PlanStep("s2", "解除卡片挂失", "unreport_card_loss"), PlanStep("s3", "核验解挂后的卡片状态", "verify_card_status")]
            else:
                summary, steps = "进入卡片管理流程，需要进一步选择操作", [PlanStep("s1", "读取卡片状态", "list_cards"), PlanStep("s2", "人工确认具体卡片操作", "human_handoff")]
        elif intent == "transfer":
            amount, name = entities.get("amount_cents"), entities.get("beneficiary")
            if not name or not isinstance(amount, int) or amount <= 0 or entities.get("amount_error"):
                summary, steps = "转账金额或收款人信息不完整，不能创建可执行转账", [PlanStep("s1", "阻止非法或不完整的转账", "clarify_transfer")]
            else:
                summary = f"向{name}转账{yuan(amount)}"
                steps = [PlanStep("s1", "核验收款人", "resolve_beneficiary"), PlanStep("s2", "检查余额和当日额度", "check_transfer_constraints"), PlanStep("s3", "创建转账", "create_transfer")]
        elif intent == "birthday_linkage":
            birthday = entities.get("birthday_date") or "待确认日期"
            summary = f"为{entities.get('recipient', '爱人')}创建生日礼物联动任务"
            steps = [PlanStep("s1", f"创建生日联动任务（生日 {birthday}）", "create_birthday_linkage")]
        elif intent == "fraud_guard":
            summary = "拦截疑似诈骗转账，并启动保护性冷静期"
            hours = entities.get("cooling_off_hours", 24)
            steps = [PlanStep("s1", "识别可疑转账风险", "detect_fraud_signals"), PlanStep("s2", f"设置{hours}小时冷静期", "create_cooling_off"), PlanStep("s3", "通知守护家人", "notify_guardian")]
        elif intent == "financial_health":
            action = entities.get("action", "report")
            if action == "savings_goal":
                summary, steps = "建立攒钱目标（只记录计划，不自动扣款）", [PlanStep("s1", "查看已有攒钱目标", "list_savings_goals"), PlanStep("s2", "建立新的攒钱目标", "create_savings_goal")]
            elif action == "idle_cash":
                summary, steps = "检查闲置资金并给出安排建议", [PlanStep("s1", "生成财务健康摘要", "financial_health_report"), PlanStep("s2", "检查闲钱", "idle_cash_check")]
            else:
                summary, steps = "生成消费体检和财务健康报告", [PlanStep("s1", "生成财务健康报告", "financial_health_report")]
        elif intent == "family_co_management":
            action = entities.get("action", "overview")
            if action == "set_allowance":
                summary, steps = "核验家庭授权后设置零花钱上限", [PlanStep("s1", "查看家庭授权关系", "list_family_relationships"), PlanStep("s2", "核验本次操作授权", "family_auth_check"), PlanStep("s3", "设置每月零花钱上限", "set_family_allowance")]
            elif action == "aa_settlement":
                summary, steps = "计算家庭 AA 分摊（只计算，不自动扣款）", [PlanStep("s1", "查看家庭成员关系", "list_family_relationships"), PlanStep("s2", "计算 AA 分摊", "calculate_aa_settlement")]
            else:
                summary, steps = "查看家庭成员关系和可操作范围", [PlanStep("s1", "读取家庭成员与授权范围", "list_family_relationships")]
        elif intent in POLICY["red"]:
            summary, steps = "进入卡片管理流程，需要进一步选择操作", [PlanStep("s1", "读取卡片状态", "list_cards"), PlanStep("s2", "人工确认具体卡片操作", "human_handoff")]
            if actor_id in self.bank.guardianships.accounts or intent == 'password_change':
                summary, steps = "本人强认证后转官方人工办理；演示系统不修改密码或卡片", [PlanStep('s1', '本人认证后人工办理', 'human_handoff')]
        else:
            summary, steps = "我可以帮你查询余额、分析账单、管理订阅、查询理财、反诈守护或发起转账", [PlanStep("s1", "澄清业务意图", "clarify")]
            if entities.get('clarification') == 'card_display_only':
                summary = '请打开“显示与帮助设置”，在本页卡片列表选择仅隐藏或恢复显示；仅改变此浏览器的展示，不挂失、不注销、不删除账户，不影响交易和安全核验。涉及其他卡片业务请单独明确说明。'
        created_at = now_iso()
        token = secrets.token_urlsafe(32)
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat(timespec="seconds")
        canonical = json.dumps({"plan_id": plan_id, "intent": intent, "entities": entities, "steps": [asdict(x) for x in steps]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        return AgentPlan(plan_id, trace_id, intent, summary, policy["risk_level"], policy["requires_confirmation"], policy["requires_strong_auth"], entities, steps, created_at, token, expires_at, actor_id, digest, device_id, risk_assessment=policy)


# ------------------------------ structured strong-auth credentials -----------------------------
@dataclass(frozen=True)
class AuthBinding:
    user_id: str
    device_id: str
    task_id: str
    node_id: str
    operation_type: str
    amount_cents: int
    recipient: str

@dataclass
class AuthCredential:
    credential_id: str
    binding: AuthBinding
    operation_digest: str
    created_at: datetime
    expires_at: datetime
    nonce: str
    used_at: Optional[datetime] = None
    attempts: int = 0
    locked: bool = False
    invalidated_reason: str = ""
    passed: bool = False

class AuthCredentialStore:
    """单机演示凭证簿；生产替换为带原子 compare-and-set 的认证服务/Redis。"""
    def __init__(self):
        self._items: Dict[str, AuthCredential] = {}
        self._lock = threading.RLock()
        self.clock = lambda: datetime.now(timezone.utc)

    def issue(self, binding: AuthBinding, operation_digest: str) -> AuthCredential:
        now = self.clock()
        item = AuthCredential(f"cred_{secrets.token_urlsafe(18)}", binding, operation_digest, now,
                              now + timedelta(seconds=STRONG_AUTH_TTL_SECONDS), secrets.token_urlsafe(24))
        with self._lock:
            self._items[item.credential_id] = item
        return item

    def get(self, credential_id: str) -> Optional[AuthCredential]:
        with self._lock:
            return self._items.get(credential_id)

    def latest_for(self, task_id: str) -> Optional[AuthCredential]:
        with self._lock:
            items = [x for x in self._items.values() if x.binding.task_id == task_id]
            return max(items, key=lambda x: x.created_at) if items else None

    def check(self, item: AuthCredential, binding: AuthBinding, operation_digest: str) -> str:
        with self._lock:
            now = self.clock()
            if item.invalidated_reason:
                return "AUTH_INVALIDATED"
            if item.locked:
                return "AUTH_LOCKED"
            if item.used_at is not None:
                return "AUTH_REPLAY"
            if now >= item.expires_at:
                item.invalidated_reason = "expired"
                return "AUTH_EXPIRED"
            if item.binding != binding or not hmac.compare_digest(item.operation_digest, operation_digest):
                item.invalidated_reason = "binding_changed"
                return "AUTH_BINDING_MISMATCH"
            return "OK"

    def fail_attempt(self, item: AuthCredential) -> int:
        with self._lock:
            item.attempts += 1
            if item.attempts >= POLICY["auth_max_attempts"]:
                item.locked = True
            return item.attempts

    def pass_auth(self, item: AuthCredential) -> str:
        with self._lock:
            status = self.check(item, item.binding, item.operation_digest)
            if status != "OK":
                return status
            item.passed = True
            return "OK"

    def consume(self, item: AuthCredential) -> str:
        with self._lock:
            status = self.check(item, item.binding, item.operation_digest)
            if status != "OK":
                return status
            item.used_at = self.clock()
            return "OK"

# ------------------------------ agent runtime -----------------------------
class AgentRuntime(RecoveryMixin):
    def __init__(self, db_path: Optional[Path] = None, offline: bool = False, features_path: Optional[Path] = None, bank_service=None):
        identity_mode=os.getenv('IDENTITY_MODE','demo')
        if identity_mode not in {'demo','self-hosted'} or identity_mode=='self-hosted' and os.getenv('APP_ENV','demo').lower() in {'prod','production'}:
            raise ValueError('身份模式无效；本机自建模式尚未取得生产部署认证。')
        # P07 is a protocol demonstration, never a live banking activation switch.
        mode=os.getenv('BANK_SERVICE_MODE','mock')
        if mode not in {'mock','protocol-test'} or mode=='protocol-test' and bank_service is None:
            raise ValueError('银行服务未配置；不支持live模式或静默回落Mock。')
        if bank_service is not None and (not isinstance(bank_service,BankServiceAdapter) or
                getattr(bank_service.transport,'scope',None) not in {'LOCAL_PROTOCOL_TEST','FINERACT_LOCAL_SIMULATOR'} or not offline or
                os.getenv('APP_ENV','demo').lower() in {'prod','production'}):
            raise ValueError('P07仅支持显式注入的离线本地协议演示，真实银行/生产启用仍未验收。')
        self.bank_service=bank_service
        # C6 演练显式注入存储，禁止修改全局路径或共用真实演示账户。
        storage_path = db_path if db_path is not None else DB_PATH
        self._dag_pause_requests = set()
        self.dag_enabled = load_dag_enabled(features_path or FEATURES_PATH)
        self.audit = AuditStore(storage_path)
        self.handoffs = HandoffQueue(self.audit)
        self.live_support = LiveSupport(self.handoffs)
        self.ocr = OCRService()
        self.voice_evidence = VoiceEvidenceStore()
        self.domain_agents = DOMAIN_AGENT_REGISTRY
        self.supervisor = Supervisor(self.domain_agents)
        self.dag_store = TaskDagStore(self.audit) if self.dag_enabled else None
        self.audit.linkage_dag_store = None  # legacy table is read-only when DAG is enabled
        self.breaker = CircuitBreaker()
        self.fraud_rules = load_fraud_rules()
        self.bank = MockBankCore(self.fraud_rules, db_path=storage_path)
        if bank_service is not None:
            bank_service.audit=lambda ctx,event,data:self.audit.write(ctx.trace_id,event,dict(data,device_id=ctx.device_id),actor=ctx.actor_id)
            self.bank=ProtocolBankFacade(self.bank,bank_service,ToolResult)
        self.conversation_context = TransferDraftStore(self.bank.beneficiaries)
        self.parser = IntentParser(self.fraud_rules)
        if offline:
            self.parser.llm = LLMIntentAdapter(LLMConfig())
        self.knowledge = KnowledgeService.from_path(ROOT / 'config' / 'knowledge.json', policy=POLICY,
            embedding_client=EmbeddingClient.from_env(offline=offline))
        self.knowledge_use_model = not offline and os.getenv('RAG_USE_MODEL', '0') == '1'
        self.policy = PolicyEngine(self.bank)
        self.planner = Planner(self.parser, self.policy, self.bank)
        self.pending: Dict[str, AgentPlan] = {}
        self.authenticated: set[str] = set()
        self.executing: set[str] = set()
        self.consumed_tokens: set[str] = set()
        self.consumed_confirmations: Dict[str, Dict[str, Any]] = {}
        self.auth_credentials = AuthCredentialStore()
        self.authenticated_credential: Dict[str, str] = {}
        self._execution_authorized: set[str] = set()
        self.auth_plan_snapshots: Dict[str, AgentPlan] = {}
        self.frozen_users: set[str] = set()
        self._tamper_traces: set[str] = set()
        # Transport uncertainty is persisted as recovery metadata, never as a
        # core ledger state.  It tells reconciliation why an in-flight action
        # is unknown without exposing adapter exception text to users.
        self._unknown_outcome_origins: Dict[str, str] = {}
        self.lock = threading.Lock()
        self.notifications = NotificationBus(self.audit, sanitize_audit_value, self.bank.guardian_targets)
        self.family = FamilyAccess(self)
        self.identity_service = IdentityService(self) if identity_mode=='self-hosted' else None
        self.init_recovery(storage_path, AgentPlan, PlanStep, AuthCredential, AuthBinding)
        if self.dag_store is not None:
            migrate_linkage_to_dag(self.audit, self.dag_store)
            for dag in self.dag_store.list_all():
                if dag.metadata.get('source') == 'linkage_tasks' and dag.metadata.get('execution_owner') == 'legacy':
                    self.bank.import_linkage_evidence(linkage_view(dag))
                    dag.metadata.update(execution_owner='dag', device_id='demo-device')
                    self.dag_store.save(dag, {'event_type': 'dag.adopted'})
        self.restore_scheduled_tasks()

    def _publish_risk_event(self, plan: AgentPlan, event_type: str, reason: str, suffix: str = "") -> Dict[str, Any]:
        return self.notifications.publish(event_type, plan.actor_id, plan.trace_id,
            {"task_id": plan.plan_id, "device_id": plan.device_id, "operation": plan.intent,
             "risk_level": plan.risk_level, "risk_score": plan.risk_assessment.get("risk_score", 0),
             "decision_reason": reason}, f"{plan.plan_id}:{event_type}:{suffix}")

    def _record_policy(self, plan: AgentPlan, decision: Dict[str, Any], stage: str) -> None:
        plan.risk_assessment = decision
        plan.risk_level = decision["risk_level"]
        plan.requires_confirmation = decision["requires_confirmation"]
        plan.requires_strong_auth = decision["requires_strong_auth"]
        payload = dict(decision, task_id=plan.plan_id, device_id=plan.device_id, stage=stage)
        self.audit.write(plan.trace_id, "policy.decided", payload, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
        if decision.get("aml_suspect"):
            self.audit.write(plan.trace_id, "aml.suspect", payload, actor=plan.actor_id, risk_level="red", intent=plan.intent)
        if decision.get('authz_allowed') is not False:
            ctx = self._domain_context(plan)
            if plan.intent in {'transfer', 'fraud_guard'}:
                self._observe_domain(plan, 'antifraud', ctx, lambda: policy_snapshot(ctx, decision))
            family = plan.entities.get('_family')
            if family:
                self._observe_domain(plan, 'family', ctx, lambda: family_snapshot(ctx,
                    self.bank.guardianships.get(family.get('grant_id', '')), self.bank.guardianships.clock()))

    @staticmethod
    def _domain_context(plan):
        family = plan.entities.get('_family', {})
        entities = {k: plan.entities[k] for k in ('amount_cents','beneficiary','subscription_id','card_id','card_operation') if k in plan.entities}
        if plan.intent == 'subscription_cancel':
            entities['operation'] = 'cancel'
        return DomainContext(plan.trace_id, 'plan-' + plan.plan_id, family.get('guardian_id', plan.actor_id),
            plan.actor_id, family.get('guardian_device_id', plan.device_id), 'internal_tool', '', entities)

    def _observe_domain(self, plan, domain, ctx, snapshot_factory):
        # T11 observes pre-existing facts, never reads a tool or executes proposals.
        # Optional analysis after a tool must not erase its actual result or retry it.
        try:
            review = self.supervisor.observe(ctx, domain, snapshot_factory())
            finding, proposal = (message.payload for message in review.messages)
            if domain in {'subscription', 'card'}:
                choices = []
                values = next((f['value'] for f in finding.facts if f.get('name') == ('subscriptions' if domain == 'subscription' else 'cards')), ())
                for item in values:
                    item_id = item.get('id')
                    if not isinstance(item_id, str) or not re.fullmatch(r'(?:sub|card)_[A-Za-z0-9_-]+', item_id):
                        continue
                    if sum(other.get('id') == item_id for other in values) != 1:
                        continue
                    if domain == 'subscription':
                        choices.append({'label':item.get('merchant') or '待核验订阅',
                            'detail': '可申请停止扣费' if item.get('terminable') and item.get('debit_state') == 'active' else '需核实扣费状态或办理渠道',
                            'request':'取消订阅编号' + item_id if item.get('terminable') and item.get('debit_state') == 'active' else ''})
                    else:
                        status = item.get('status')
                        operation = '挂失' if status in ('normal','active','正常') else '解挂' if status in ('reported_loss','挂失') else ''
                        distinguishable = sum(card_label(other) == card_label(item) for other in values) == 1
                        choices.append({'label':card_label(item),
                                        'detail': ('已挂失' if operation == '解挂' else '正常' if operation else '状态待核实') if distinguishable else '卡片信息相同，请联系人工核实',
                                        'request':operation + '卡片编号' + item_id if operation and distinguishable else ''})
                plan.coordination['domain_guidance'] = {
                    'domain':domain, 'title':'订阅办理核验' if domain == 'subscription' else '卡片办理核验',
                    'notes':list(finding.recommendations), 'choices':choices,
                    'phase':'本次查询时的资料；办理结果以回执为准',
                }
                plan.coordination['domain_review'] = {'domain':domain, 'ready':review.status == 'ready',
                                                     'tools':[s['tool'] for s in proposal.suggested_steps],
                                                     'signals':list(finding.risk_signals)}
            envelope = review.messages[0]
            self.audit.write(plan.trace_id, 'domain.' + domain + '.analyzed', {
                'mode':'ADVISORY_ONLY', 'domain':domain, 'fact_count':len(finding.facts),
                'context_key':envelope.context_key.translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop')),
                'evidence':list(finding.evidence), 'missing_fields':list(finding.missing_fields),
                'risk_signals':list(finding.risk_signals), 'suggested_tools':[s['tool'] for s in proposal.suggested_steps]},
                actor=plan.actor_id, intent=plan.intent)
            self.audit.write(plan.trace_id, 'supervisor.evidence.verified',
                {'domain':domain,'phase':'existing_evidence','status':review.status,
                 'conflict_keys':[c['key'] for c in review.conflicts]}, actor=plan.actor_id, intent=plan.intent)
        except Exception:
            if domain in {'subscription', 'card'}:
                plan.coordination['domain_review'] = {'domain':domain, 'ready':False, 'tools':[]}
                plan.coordination['domain_guidance'] = {'domain':domain,'title':'办理资料需核实',
                    'notes':['领域核验暂不可用，后续写操作已停止；已取得的工具回执仍保留。'], 'choices':[], 'phase':'请核实后重新办理'}
            self.audit.write(plan.trace_id, 'domain.' + domain + '.unavailable',
                {'mode':'ADVISORY_ONLY', 'reason':'domain_analysis_unavailable', 'bank_result_unchanged':True}, actor=plan.actor_id)

    def _defer_transfer(self, plan: AgentPlan) -> Dict[str, Any]:
        with self.lock:
            self.pending.pop(plan.plan_id, None)
        reason = plan.risk_assessment["decision_reason"]
        self.audit.write(plan.trace_id, "transaction.deferred", {"task_id": plan.plan_id, "risk_score": plan.risk_assessment["risk_score"], "decision_reason": reason}, actor=plan.actor_id, risk_level="red", intent=plan.intent)
        self._publish_risk_event(plan, "transaction.deferred", reason)
        message = "交易已暂缓，未发生扣款，请联系人工核实；通知状态可在通知记录中查看。"
        result = {"ok": False, "status": "DEFERRED", "error": message, "trace_id": plan.trace_id}
        return self._response(plan, message, result, blocked=True)

    def _create_handoff_card(self, trigger: str, *, plan: Any = None, dag: Any = None,
                             actor_id: str = "", device_id: str = "", trace_id: str = "",
                             request_id: str = "", pending_items: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create and audit a least-privilege handoff card.

        This is an evidence handoff only: it grants no authority and contains no
        raw conversation or authentication material. Production replacement
        point: submit the same contract to the bank CRM/case API.
        """
        if dag is not None:
            raw = HandoffCard.from_dag(dag, trigger, pending_items=pending_items)
        elif plan is not None:
            raw = HandoffCard.from_plan(plan, trigger, pending_items=pending_items)
        else:
            raw = HandoffCard.for_request(actor_id, device_id, trace_id, request_id, trigger)
        card = raw.to_dict()
        event_trace = trace_id or card.get("business_identifiers", {}).get("trace_id") or "handoff_unknown"
        owner = actor_id or (getattr(plan, 'actor_id', '') if plan is not None else getattr(dag, 'actor_id', ''))
        device = device_id or (getattr(plan, 'device_id', '') if plan is not None else (getattr(dag, 'metadata', {}) or {}).get('device_id', ''))
        case = self.handoffs.enqueue(sanitize_audit_value(card), owner, device, event_trace, trigger)
        card['local_case_id'] = case['case_id']
        self.audit.write(event_trace, "human.handoff.created", {"card": card, "trigger": trigger},
                         actor=actor_id or card.get("authorization_scope", {}).get("actor_id", "system"),
                         risk_level="yellow", intent="human_handoff")
        return card

    def request_customer_service(self, request_id: str, actor_id: str = "demo-user", device_id: str = "demo-device") -> Dict[str, Any]:
        # Request IDs are retry keys, never authority or caller-selected audit traces.
        if any(not isinstance(v, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", v) for v in (request_id, actor_id, device_id)):
            raise ValueError("请提供有效的客服请求标识。")
        trace = "support_" + hashlib.sha256((actor_id + ":" + request_id).encode()).hexdigest().translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
        with self.notifications.lock:
            event = self.notifications.publish("customer_service.requested", actor_id, trace,
                {"device_id": device_id, "operation": "customer_service", "risk_level": "green",
                 "decision_reason": "用户主动请求人工帮助；仅提交本地客服请求，未接通真人"}, "support:" + request_id)
            if not any(e["event_type"] == "customer_service.requested" for e in self.audit.list_for_trace(trace)):
                self.audit.write(trace, "customer_service.requested", {"event_id": event["event_id"], "device_id": device_id,
                    "decision_reason": "本地客服请求已记录，尚未接通真人客服"}, actor=actor_id, risk_level="green", intent="customer_service")
        result = {"ok": True, "status": "LOCAL_QUEUED", "event_id": event["event_id"], "trace_id": trace,
                  "message": "人工客服请求已在本地记录，尚未接通真人客服。请通过银行卡背面或银行官方 App 联系官方客服。",
                  "notification_count": len(self.notifications.list_notifications(actor_id, trace))}
        # A failure to build the optional evidence card must never block the
        # already-recorded support request.
        try:
            result["handoff_card"] = self._create_handoff_card(
                "user_requested", actor_id=actor_id, device_id=device_id, trace_id=trace, request_id=request_id)
            result['handoff_case'] = self.handoffs.status(result['handoff_card']['local_case_id'], actor_id, device_id)
        except Exception:
            pass
        return result

    @staticmethod
    def _due_at(birthday_date: str, days_before: int) -> str:
        due = date.fromisoformat(birthday_date) - timedelta(days=days_before)
        return f"{due.isoformat()}T00:00:00+00:00"

    @staticmethod
    def _is_due(due_at: str, now: str) -> bool:
        try:
            return datetime.fromisoformat(due_at) <= datetime.fromisoformat(now)
        except (TypeError, ValueError):
            return False

    def get_linkage_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        if self.dag_store is not None:
            dag = self.dag_store.get('linkage-' + task_id)
            if dag is not None:
                return linkage_view(dag)
        return self.audit.get_linkage_task(task_id)

    def _rehydrate_linkage_lock(self, task: Dict[str, Any]) -> None:
        node_a = next((node for node in task.get("nodes", []) if node.get("node_id") == "A"), None)
        if not node_a or node_a.get("status") != "COMPLETED":
            return
        if task.get("status") not in {"ACTIVE", "COMPLETED"}:
            return
        if task["task_id"] in self.bank.subaccount_locks:
            return
        result = self.bank.lock_subaccount(task["lock_amount_cents"], task["task_id"], task["actor_id"])
        if not result.ok:
            self.audit.write(task["trace_id"], "linkage.recovery.failed", {"task_id": task["task_id"], "error": result.error_message}, actor=task["actor_id"], intent="birthday_linkage")

    def _run_linkage_node(self, task: Dict[str, Any], node: Dict[str, Any]) -> ToolResult:
        action = node.get("action")
        if action == "order_flowers":
            result = self.bank.order_linkage_item("flowers", task["task_id"], task["actor_id"])
        elif action == "order_cake":
            result = self.bank.order_linkage_item("cake", task["task_id"], task["actor_id"])
        else:
            return ToolResult(False, action or "linkage_node", error_code="UNKNOWN_LINKAGE_NODE", error_message="未知的生日联动节点。")
        if result.ok:
            node["status"] = "COMPLETED"
            node["completed_at"] = now_iso()
            node["result"] = result.data
            self.audit.write(task["trace_id"], "linkage.node.completed", {"task_id": task["task_id"], "node_id": node["node_id"], "kind": action, "result": result.data}, actor=task["actor_id"], intent="birthday_linkage")
        else:
            node["status"] = "FAILED"
            node["error"] = result.error_message
            self.audit.write(task["trace_id"], "linkage.node.failed", {"task_id": task["task_id"], "node_id": node["node_id"], "error_code": result.error_code, "error": result.error_message}, actor=task["actor_id"], intent="birthday_linkage")
        return result

    def restore_scheduled_tasks(self, now: Optional[str] = None) -> List[str]:
        """启动恢复/测试时钟入口：加载 SQLite checkpoint 并幂等推进到期节点。"""
        if self.dag_store is not None:
            return self.advance_dags(now)
        effective_now = now or now_iso()
        completed: List[str] = []
        for task in self.audit.list_linkage_tasks():
            # A flag rollback must never hand adopted tasks to the old scheduler.
            if self._dag_adopted(task['task_id']):
                continue
            if task.get("status") not in {"ACTIVE", "COMPLETED"}:
                continue
            self._rehydrate_linkage_lock(task)
            for completed_node in task.get("nodes", []):
                if completed_node.get("status") != "COMPLETED" or completed_node.get("node_id") not in {"B", "C"}:
                    continue
                kind = "flowers" if completed_node.get("node_id") == "B" else "cake"
                if not any(order.get("task_id") == task["task_id"] and order.get("kind") == kind for order in self.bank.linkage_orders):
                    self.bank.order_linkage_item(kind, task["task_id"], task["actor_id"])
            self.audit.write(task["trace_id"], "linkage.recovered", {"task_id": task["task_id"], "now": effective_now}, actor=task["actor_id"], intent="birthday_linkage")
            changed = False
            for node in task.get("nodes", []):
                if node.get("status") != "PENDING" or not self._is_due(node.get("due_at", ""), effective_now):
                    continue
                self._run_linkage_node(task, node)
                changed = True
                self.audit.save_linkage_task(task)
            if all(node.get("status") == "COMPLETED" for node in task.get("nodes", [])):
                task["status"] = "COMPLETED"
                changed = True
            if changed:
                self.audit.save_linkage_task(task)
            if any(node.get("status") == "COMPLETED" for node in task.get("nodes", []) if node.get("node_id") in {"B", "C"}):
                completed.append(task["task_id"])
        return completed

    def create_birthday_linkage(self, birthday_date: str, actor_id: str, trace_id: str, plan_id: str = "",
                                flowers_days_before: int = 2, cake_days_before: int = 1) -> ToolResult:
        schedule_error = IntentParser.birthday_schedule_error(birthday_date, flowers_days_before, cake_days_before)
        if schedule_error:
            return ToolResult(False, 'create_birthday_linkage', error_code='INVALID_BIRTHDAY_SCHEDULE', error_message=schedule_error)
        try:
            birthday = date.fromisoformat(birthday_date)
        except (TypeError, ValueError):
            return ToolResult(False, "create_birthday_linkage", error_code="INVALID_BIRTHDAY_DATE", error_message="生日日期无效，请补充明确日期。")
        if birthday < date.fromisoformat(today_key()):
            return ToolResult(False, "create_birthday_linkage", error_code="BIRTHDAY_IN_PAST", error_message="生日日期已过去，请重新确认日期。")
        task_id = new_id("birthday")
        lock_amount_cents = POLICY["birthday_lock_amount_cents"]
        task = {
            "task_id": task_id,
            "plan_id": plan_id or task_id,
            "trace_id": trace_id,
            "actor_id": actor_id,
            "status": "ACTIVE",
            "recipient": "爱人",
            "birthday_date": birthday.isoformat(),
            "flowers_days_before": flowers_days_before,
            "cake_days_before": cake_days_before,
            "lock_amount_cents": lock_amount_cents,
            "created_at": now_iso(),
            "nodes": [
                {"node_id": "A", "action": "lock_subaccount", "due_at": now_iso(), "status": "PENDING"},
                {"node_id": "B", "action": "order_flowers", "due_at": self._due_at(birthday.isoformat(), flowers_days_before), "status": "PENDING"},
                {"node_id": "C", "action": "order_cake", "due_at": self._due_at(birthday.isoformat(), cake_days_before), "status": "PENDING"},
            ],
        }
        if self.dag_store is not None:
            dag = linkage_to_dag(task)
            parent = self.auth_plan_snapshots.get(plan_id) or self.pending.get(plan_id)
            dag.metadata.update(execution_owner='dag', device_id=parent.device_id if parent else 'demo-device')
            self.dag_store.save(dag, {'event_type': 'dag.created'})
            self._run_birthday_dag(dag)
            view = linkage_view(dag)
            if dag.status in {'failed', 'waiting_human'}:
                return ToolResult(False, 'create_birthday_linkage', {'task_id': task_id},
                                  error_code='LINKAGE_UNRESOLVED', error_message='生日任务未完成，请联系人工核实。')
            self.audit.write(trace_id, 'linkage.created', {'task_id': task_id}, actor=actor_id, intent='birthday_linkage')
            return ToolResult(True, 'create_birthday_linkage', view)
        lock_result = self.bank.lock_subaccount(lock_amount_cents, task_id, actor_id)
        if not lock_result.ok:
            return ToolResult(False, "create_birthday_linkage", error_code=lock_result.error_code, error_message=lock_result.error_message)
        task["nodes"][0]["status"] = "COMPLETED"
        task["nodes"][0]["completed_at"] = now_iso()
        task["nodes"][0]["result"] = lock_result.data
        self.audit.save_linkage_task(task)
        self.audit.write(trace_id, "linkage.created", {"task_id": task_id, "birthday_date": birthday.isoformat(), "nodes": task["nodes"]}, actor=actor_id, intent="birthday_linkage")
        self.audit.write(trace_id, "linkage.node.completed", {"task_id": task_id, "node_id": "A", "kind": "lock_subaccount", "result": lock_result.data}, actor=actor_id, intent="birthday_linkage")
        return ToolResult(True, "create_birthday_linkage", {"task_id": task_id, "status": task["status"], "birthday_date": birthday.isoformat(), "lock_amount_cents": lock_amount_cents, "nodes": task["nodes"]})

    def cancel_linkage_task(self, task_id: str, actor_id: str = "demo-user", device_id: Optional[str] = None) -> Dict[str, Any]:
        if self.dag_store is not None:
            dag = self.dag_store.get('linkage-' + task_id)
            if dag:
                out = self.dag_control('cancel', dag.dag_id, actor_id, device_id if device_id is not None else dag.metadata.get('device_id', ''))
                return dict(out, task=self.get_linkage_task(task_id))
        if self._dag_adopted(task_id):
            return {'ok': False, 'message': '该任务已由 DAG 接管，请启用 DAG 后操作。'}
        task = self.audit.get_linkage_task(task_id)
        if not task:
            return {"ok": False, "message": "生日联动任务不存在。"}
        if task.get("actor_id") != actor_id:
            return {"ok": False, "message": "无权取消这项生日联动任务。"}
        if task.get("status") == "CANCELLED":
            return {"ok": True, "message": "生日联动任务已取消。", "task": task}
        for node in task.get("nodes", []):
            if node.get("status") == "PENDING":
                node["status"] = "CANCELLED"
                node["cancelled_at"] = now_iso()
        node_a = next((node for node in task.get("nodes", []) if node.get("node_id") == "A"), None)
        if node_a and node_a.get("status") == "COMPLETED":
            compensation = self.bank.compensate_subaccount_lock(task_id)
            if not compensation.ok:
                return {"ok": False, "message": compensation.error_message or "锁定补偿失败。"}
            node_a["status"] = "COMPENSATED"
            node_a["compensation"] = compensation.data
            self.audit.write(task["trace_id"], "linkage.compensation.completed", {"task_id": task_id, "node_id": "A", "result": compensation.data}, actor=actor_id, intent="birthday_linkage")
        task["status"] = "CANCELLED"
        self.audit.save_linkage_task(task)
        self.audit.write(task["trace_id"], "linkage.cancelled", {"task_id": task_id, "status": task["status"]}, actor=actor_id, intent="birthday_linkage")
        return {"ok": True, "message": "生日联动任务已取消，已执行的预算锁定已补偿。", "task": task}

    def query_transaction(self, transaction_id: str) -> Dict[str, Any]:
        """对外提供只读最终状态查询；生产替换点是核心查询 API。"""
        try:
            result = self.bank.query_transaction(transaction_id)
        except (TimeoutError, ConnectionError, OSError):
            view = transaction_view({"status": "unknown"})
            return {"ok": True, "status": "unknown", "transaction": None,
                    "error_code": "OUTCOME_UNKNOWN", "error": "银行暂时无法确认这笔交易。",
                    "result_view": view}
        raw_status = result.data.get("status") if result.data else "not_found"
        view = transaction_view(result.data if result.data else {"status": raw_status})
        return {
            "ok": result.ok,
            "status": raw_status,
            "transaction": result.data,
            "error_code": result.error_code,
            "error": result.error_message,
            "result_view": view,
        }

    def readiness_report(self) -> Dict[str, Any]:
        """输出可审计的商用准备度，而不是把 Demo 冒充成已获资质系统。"""
        controls = [
            {"id": "core_banking", "name": "真实银行核心与支付网关", "status": "blocked", "evidence": "本地银行协议演示，真实银行未接入" if self.bank_service is not None else "当前使用 MockBankCore"},
            {"id": "identity_mfa", "name": "实名身份、设备绑定与强认证", "status": "partial", "evidence": "仅有演示验证码"},
            {"id": "audit_worm", "name": "不可篡改审计与安全运营", "status": "partial", "evidence": "SQLite 哈希链，仅用于演示"},
            {"id": "privacy_governance", "name": "个人信息最小化、授权与留存治理", "status": "partial", "evidence": "已做最小化设计，未完成组织级合规"},
            {"id": "availability", "name": "高可用、灾备、压测和应急演练", "status": "blocked", "evidence": "单机演示服务"},
            {"id": "security_assessment", "name": "等保、渗透、代码审计和监管评估", "status": "blocked", "evidence": "需由银行和合规机构完成"},
            {"id": "channel_auth", "name": "App/IM 签名、登录态与反重放", "status": "partial", "evidence": "已提供统一契约与 HMAC 校验函数，尚未连接平台"},
            {"id": "elder_experience", "name": "适老化可理解交互", "status": "implemented", "evidence": "大字、通俗词、语音输入和确认前复述"},
            {"id": "fraud_guard", "name": "反诈守护与保护性冷静期", "status": "implemented", "evidence": f"诈骗话术、新收款人大额组合识别、{POLICY['cooling_off_hours']}小时冷静期和守护人通知"},
            {"id": "business_logic_security", "name": "业务逻辑攻击防护", "status": "implemented", "evidence": "金额 Schema 校验、一次性 consent token、幂等和副作用前重校验"},
            {"id": "card_management_write", "name": "真实卡片写操作", "status": "blocked", "evidence": "当前为 MockBankCore 卡片写操作演示；生产需接入真实核心卡管服务并完成状态核验与对账"},
        ]
        return {
            "status": "NOT_READY_FOR_PRODUCTION",
            "message": "这是竞赛演示和工程基线，不代表已取得银行商用资质。",
            "controls": controls,
            "release_blockers": [x["name"] for x in controls if x["status"] == "blocked"],
            "production_contracts": production_contract_manifest(),
        }

    def is_user_frozen(self, actor_id: str) -> bool:
        with self.lock:
            return actor_id in self.frozen_users

    def unlock_user(self, actor_id: str) -> bool:
        with self.lock:
            existed = actor_id in self.frozen_users
            self.frozen_users.discard(actor_id)
            return existed

    def verify_audit_trace(self, trace_id: str) -> Dict[str, Any]:
        result = self.audit.verify_trace(trace_id)
        if not result.get("valid"):
            user_id = str(result.get("user_id") or result.get("frozen_user") or "")
            if user_id:
                with self.lock:
                    self.frozen_users.add(user_id)
            if trace_id not in self._tamper_traces:
                self._tamper_traces.add(trace_id)
                self.audit.write(trace_id, "security.tamper", {
                    "task_id": result.get("task_id", trace_id), "user_id": user_id, "device_id": "system",
                    "break_at": result.get("break_at"), "reason": result.get("reason", "unknown"),
                    "decision_reason": "审计哈希链校验失败，冻结高风险操作，等待人工解锁",
                }, actor="system:audit-verifier", risk_level="red", intent="audit_integrity")
            result["security_event"] = "security.tamper"
            result["frozen_users"] = sorted(self.frozen_users)
        return result

    def capabilities(self) -> Dict[str, Any]:
        return {
            "agent": "banking-agent",
            "channel": "web-app",
            "model_mode": self.parser.llm.config.display_name,
            "llm_enabled": self.parser.llm.config.enabled,
            "knowledge": {"available": self.knowledge.available, "entry": "知识问答：问题",
                          "retrieval": (("local_vector" if getattr(self.knowledge.embedding_client, 'transport', '') == 'local' else "hybrid_vector")
                                        if self.knowledge.embedding_client and self.knowledge.embedding_client.enabled else "local_lexical"),
                          "model_opt_in": self.knowledge_use_model,
                          "account_facts": False},
            "mock_core": True,
            "production_notice": "当前演示使用 MockBankCore，不连接真实银行核心。",
            "security": {"tool_whitelist": True, "policy_recheck": True, "audit": True, "strong_auth_demo": self.identity_service is None, "http_headers": True, "rate_limit_demo": True, "fraud_guard": True, "business_logic_recheck": True, "consent_token": True},
            "identity": {"mode":"SELF_HOSTED" if self.identity_service else "DEMO","production_certified":False,"hardware_device_attestation":False},
            "elder_mode": {"large_text": True, "plain_language": True, "voice_input": True, "confirmation_rephrase": True},
            "scenarios": {"fraud_guard": "P0", "financial_health": "P1", "family_co_management": "P1", "birthday_linkage": "P1"},
            "production_readiness": self.readiness_report()["status"],
            "production_contracts": production_contract_manifest(),
        }

    @staticmethod
    def _response(plan: AgentPlan, message: str, result: Optional[Dict[str, Any]] = None, **extra: Any) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"ok": True, "trace_id": plan.trace_id, "message": message, "plan": plan.as_dict(), "plan_view": plan.user_view()}
        if result is not None:
            payload["result"] = result
            payload["result_view"] = present_result(plan, result)
            guidance = payload['plan_view'].get('domain_guidance')
            if guidance and plan.intent == 'subscription_cancel':
                guidance = copy.deepcopy(guidance)
                guidance['phase'] = '办理前查询的资料；是否停止扣费以本次回执为准'
                guidance['choices'] = []
                guidance['notes'] = [note for note in guidance['notes'] if not note.startswith('可以提出')]
                payload['plan_view']['domain_guidance'] = guidance
        payload.update(extra)
        # A review card is not necessarily a pending executable plan. Never advertise
        # confirmation for clarification, rejection or a completed/deferred result.
        actionable = (plan.requires_confirmation and result is None
                      and plan.intent != "unknown" and not extra.get("blocked")
                      and not extra.get("awaiting_details"))
        payload["plan_view"]["requires_confirmation"] = bool(actionable)
        if not actionable:
            payload["plan_view"]["action_token"] = ""
            payload["plan_view"]["confirmation_hint"] = ""
        payload["plan_view"]["awaiting_details"] = bool(extra.get("awaiting_details"))
        payload["plan_view"]["deferred"] = bool(result and result.get("status") == "DEFERRED")
        return payload

    def _refresh_payment_preflight(self, plan: AgentPlan) -> Dict[str, Any]:
        """在确认/副作用边界重新读取预算事实；计划卡不是执行保证。"""
        report = build_payment_preflight(
            self.bank,
            POLICY,
            amount_cents=plan.entities.get("amount_cents"),
            beneficiary=plan.entities.get("beneficiary"),
            actor_id=plan.actor_id,
            account_requires_verification=bool(plan.entities.get("account_requires_verification")),
            requires_confirmation=plan.requires_confirmation,
            requires_strong_auth=plan.requires_strong_auth,
        )
        plan.preflight = report.to_dict()
        self.audit.write(
            plan.trace_id,
            "payment.preflight",
            {"plan_id": plan.plan_id, "operation": "transfer", "preflight": plan.preflight, "phase": "confirmation"},
            actor=plan.actor_id,
            risk_level=plan.risk_level,
            intent=plan.intent,
        )
        return plan.preflight

    def _transfer_details(self, plan: AgentPlan, message: str) -> Dict[str, Any]:
        self.audit.write(plan.trace_id, "transfer.details_required", {
            "plan_id": plan.plan_id, "reason": "recipient_or_amount_requires_review",
        }, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
        return self._response(plan, message, awaiting_details=True, transfer_draft={
            "beneficiary": plan.entities.get("beneficiary"),
            "account_requires_verification": bool(plan.entities.get("account_requires_verification")),
            "amount_cents": plan.entities.get("amount_cents"),
            "note": plan.entities.get("note", ""),
        })

    def _validate_token(self, plan_id: str, action_token: str) -> Tuple[Optional[AgentPlan], Optional[Dict[str, Any]]]:
        with self.lock:
            plan = self.pending.get(plan_id)
            if not plan:
                consumed = self.consumed_confirmations.get(plan_id)
                digest = hashlib.sha256(str(action_token).encode()).hexdigest()
                if consumed and hmac.compare_digest(consumed["token_digest"], digest):
                    self.audit.write(consumed["trace_id"], "consent.replay", {"plan_id": plan_id, "reason": "already_consumed"}, actor=consumed["actor_id"], risk_level=consumed["risk_level"], intent=consumed["intent"])
                return None, {"ok": False, "message": "操作计划不存在、已完成或已过期。"}
            if not action_token or not hmac.compare_digest(str(plan.consent_token), str(action_token)):
                self.audit.write(plan.trace_id, "consent.rejected", {"plan_id": plan_id, "reason": "invalid_action_token"}, risk_level=plan.risk_level, intent=plan.intent)
                return None, {"ok": False, "message": "操作凭据无效，不能执行这笔操作。"}
            try:
                if datetime.now(timezone.utc) >= datetime.fromisoformat(plan.token_expires_at):
                    self.pending.pop(plan_id, None)
                    return None, {"ok": False, "message": "操作确认已过期，请重新发起需求。"}
            except (TypeError, ValueError):
                self.pending.pop(plan_id, None)
                return None, {"ok": False, "message": "操作确认已失效，请重新发起需求。"}
            if plan.consent_token in self.consumed_tokens:
                self.audit.write(plan.trace_id, "consent.replay", {"plan_id": plan_id, "reason": "already_consumed"}, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
                return None, {"ok": False, "message": "这张确认卡片已经使用过，不能重复执行。"}
            if plan_id in self.executing:
                self.audit.write(plan.trace_id, "consent.replay", {"plan_id": plan_id, "reason": "in_progress"}, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
                return None, {"ok": False, "message": "这笔操作正在处理中，请不要重复点击。"}
            canonical = json.dumps({"plan_id": plan.plan_id, "intent": plan.intent, "entities": plan.entities, "steps": [asdict(x) for x in plan.steps]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            if not hmac.compare_digest(plan.entity_digest, hashlib.sha256(canonical.encode("utf-8")).hexdigest()):
                credential = self.auth_credentials.get(self.authenticated_credential.get(plan_id, ""))
                if credential is not None:
                    credential.invalidated_reason = "binding_changed"
                    self.audit.write(plan.trace_id, "auth.invalidated", {"plan_id": plan_id, "credential_id": credential.credential_id, "reason": "binding_changed"}, risk_level=plan.risk_level, intent=plan.intent)
                return None, {"ok": False, "error_code": "AUTH_BINDING_MISMATCH", "message": "操作内容已变化，请重新发起需求。"}
            return plan, None

    def _invalid_input(self, plan: AgentPlan) -> bool:
        if plan.intent == "transfer":
            return not plan.entities.get("beneficiary") or not isinstance(plan.entities.get("amount_cents"), int) or plan.entities.get("amount_cents", 0) <= 0 or bool(plan.entities.get("amount_error"))
        if plan.intent == "financial_health" and plan.entities.get("action") == "savings_goal":
            return not isinstance(plan.entities.get("target_cents"), int) or plan.entities.get("target_cents", 0) <= 0 or bool(plan.entities.get("amount_error"))
        if plan.intent == "family_co_management" and plan.entities.get("action") == "set_allowance":
            return not isinstance(plan.entities.get("allowance_cents"), int) or plan.entities.get("allowance_cents", 0) <= 0 or bool(plan.entities.get("amount_error"))
        return False

    @audit_action
    @durable_action
    def chat(self, text: str, actor_id: str = "demo-user", session_id: str = "web-demo", device_id: str = "demo-device", family_context=None, trace_id: Optional[str] = None, channel: str = "internal") -> Dict[str, Any]:
        if self.identity_service:
            identity=self.identity_service.current_identity()
            if not identity or identity['role']!='user' or identity['user_id']!=actor_id or actor_id!='demo-user' or identity['device_id']!=device_id or family_context is not None:
                return self.family.deny('当前身份、设备或账户授权未通过，不能提交业务。',actor=actor_id,device=device_id)
        # Conversation identifiers scope memory, never identity or permission.
        key = (actor_id, device_id, channel, session_id)
        with self.conversation_context.turn(key) as context:
            if family_context is not None or actor_id in self.bank.guardianships.accounts:
                context = None  # family/delegated drafts need a separate reviewed contract
            return self._chat(text, actor_id, session_id, device_id, family_context, trace_id, context, channel)

    def _chat(self, text, actor_id, session_id, device_id, family_context, trace_id, context, channel):
        trace = trace_id or new_id("trace")
        if self.breaker.is_locked():
            self.audit.write(trace, "request.blocked", {"reason": "circuit_breaker_locked"}, actor=actor_id)
            return {"ok": False, "trace_id": trace, "message": "安全熔断已触发，当前会话已锁定，请联系人工客服。", "breaker": self.breaker.state()}
        allowed, reason = InjectionGuard.inspect(text)
        if not allowed:
            self.audit.write(trace, "injection_blocked", {"input_length": len(text), "reason": reason}, actor=actor_id)
            return {"ok": False, "trace_id": trace, "message": reason, "blocked": True}
        # 离线最小披露规则：不回显原始敏感输入，不把泄露请求交给模型。
        if re.search(r"(系统提示词|系统提示|内部提示词)|((完整|全部|明文|不.{0,2}脱敏).{0,12}(卡号|身份证))|((卡号|身份证).{0,12}(完整|全部|明文|不.{0,2}脱敏))", text):
            self.audit.write(trace, "data.disclosure.blocked", {"input_length": len(text), "reason": "sensitive_disclosure_request"}, actor=actor_id)
            return {"ok": False, "trace_id": trace, "message": "为保护隐私，不能提供完整卡号、身份证号码或内部系统提示词。可以查询脱敏的卡片状态。", "blocked": True}
        # 仅可信入口分配 trace；计划、工具与审计沿用同一条回放链。
        resolved = context.resolve(text) if context is not None else text
        plan = self.planner.build(resolved, actor_id=actor_id, device_id=device_id, family_context=family_context, trace_id=trace)
        if resolved != text:
            self.audit.write(trace, 'context.draft.applied', {'scope': 'transfer_clarification',
                             'requires_fresh_confirmation': True}, actor=actor_id)
        self.audit.write(plan.trace_id, "input.received", {"input_length": len(text)}, actor=plan.actor_id)
        self.audit.write(plan.trace_id, "injection.checked", {"allowed": True}, actor=plan.actor_id)
        self.audit.write(plan.trace_id, "parser.result", {
            **self.parser.llm.evidence, "plan_id": plan.plan_id, "intent": plan.intent,
        }, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
        self.audit.write(plan.trace_id, "plan.created", {"input_length": len(text), "plan": plan.as_dict(include_token=False)}, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
        self._record_policy(plan, plan.risk_assessment, "planning")
        if plan.risk_assessment.get("authz_allowed") is False:
            return self.family.deny(plan.risk_assessment["decision_reason"], plan)
        if plan.intent == "unknown":
            return self._response(plan, plan.summary)
        if plan.intent == 'birthday_linkage' and plan.entities.get('schedule_error'):
            return self._response(plan, plan.entities['schedule_error'], blocked=True)
        if plan.intent == "subscription_cancel" and not plan.entities.get("subscription_id"):
            result = self.execute(plan)  # read-only lookup followed by a rejected cancellation step
            response = self._response(plan, result['error'], result, blocked=True)
            response['ok'] = False
            return response
        if self._invalid_input(plan):
            if context is not None and plan.intent == 'transfer':
                context.remember(plan.entities)
                if context.next_draft is not None:
                    missing = ('收款人和转账金额' if not plan.entities.get('beneficiary') and not plan.entities.get('amount_cents')
                               else '收款人' if not plan.entities.get('beneficiary') else '转账金额')
                    response = self._transfer_details(plan, f'请补充{missing}。补齐后会重新展示计划，确认前不会转账。')
                    response['conversation_followup'] = True
                    return response
            message = plan.entities.get("amount_error") or "信息不完整或金额不合法，未创建任何转账或扣款。"
            self.audit.write(plan.trace_id, "business_input.blocked", {"message": message, "entities": {k: v for k, v in plan.entities.items() if k != "raw"}}, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
            if plan.intent == "transfer" and not plan.entities.get("amount_error"):
                return self._transfer_details(plan, "请补充收款账户和转账金额；信息齐全后再核对确认，目前没有扣款。")
            return self._response(plan, message, blocked=True)
        if plan.intent == "transfer" and strict_positive_int(plan.entities.get("amount_cents")):
            preflight = build_payment_preflight(
                self.bank,
                POLICY,
                amount_cents=plan.entities["amount_cents"],
                beneficiary=plan.entities.get("beneficiary"),
                actor_id=plan.actor_id,
                account_requires_verification=bool(plan.entities.get("account_requires_verification")),
                requires_confirmation=plan.requires_confirmation,
                requires_strong_auth=plan.requires_strong_auth,
            )
            plan.preflight = preflight.to_dict()
            self.audit.write(
                plan.trace_id,
                "payment.preflight",
                {"plan_id": plan.plan_id, "operation": "transfer", "preflight": plan.preflight},
                actor=plan.actor_id,
                risk_level=plan.risk_level,
                intent=plan.intent,
            )
            # T10: reviewed payment sample observes the existing preflight only.
            # Findings/proposals are not executable plans or authorization.
            try:
                family = family_context or {}
                domain_context = DomainContext(plan.trace_id, session_id,
                    family.get('guardian_id', actor_id), plan.actor_id,
                    family.get('guardian_device_id', device_id), channel,
                    text[:2000], {k: plan.entities.get(k) for k in
                        ('amount_cents', 'beneficiary', 'account_requires_verification')})
                sources = {'payment':PaymentSnapshot.from_preflight(domain_context, preflight),
                    'antifraud':policy_snapshot(domain_context, plan.risk_assessment),
                    'finance_health':budget_snapshot(domain_context, preflight)}
                if domain_context.actor_id != domain_context.subject_id:
                    sources['family'] = family_snapshot(domain_context,
                        self.bank.guardianships.get(family.get('grant_id', '')), self.bank.guardianships.clock())
                review = self.supervisor.coordinate(domain_context, plan.intent, sources)
                plan.coordination = review.summary()
                finding, proposal = (message.payload for message in review.messages if message.sender == 'payment')
                envelope = next(message for message in review.messages if message.sender == 'payment')
                self.audit.write(plan.trace_id, 'domain.payment.analyzed', {
                    'mode': 'ADVISORY_ONLY', 'domain': finding.domain,
                    'context_key': envelope.context_key.translate(str.maketrans('0123456789abcdef', 'abcdefghijklmnop')),
                    'fact_count': len(finding.facts), 'evidence': list(finding.evidence),
                    'missing_fields': list(finding.missing_fields), 'risk_signals': list(finding.risk_signals),
                    'suggested_tools': [step['tool'] for step in proposal.suggested_steps],
                    'required_confirmations': list(proposal.required_confirmations),
                    'required_auth': list(proposal.required_auth)}, actor=plan.actor_id, intent='transfer')
                self.audit.write(plan.trace_id, 'supervisor.reviewed',
                    {'plan_id':plan.plan_id, **review.summary()}, actor=plan.actor_id, intent=plan.intent)
                if review.status == 'ready':
                    self.supervisor.review_plan(domain_context, review, plan.entities, [s.tool for s in plan.steps])
                    self.audit.write(plan.trace_id, 'supervisor.plan.checked',
                        {'plan_id':plan.plan_id,'template':'existing_transfer','tools':list(review.reviewed_tools)},
                        actor=plan.actor_id, intent=plan.intent)
                elif review.status == 'needs_review':
                    response = self._response(plan, review.summary()['message'], blocked=True,
                        review_options=['recheck_facts','human_handoff'])
                    response['ok'] = False
                    return response
            except Exception:
                plan.coordination = {**plan.coordination, 'status':'needs_review', 'mode':'REVIEW_ONLY',
                    'reviewed_tools':[], 'message':'资料暂时无法核对，请稍后或联系人工。'}
                self.audit.write(plan.trace_id, 'domain.payment.failed',
                    {'reason': 'payment_advisory_unavailable', 'mode': 'ADVISORY_ONLY'}, actor=plan.actor_id)
                self.audit.write(plan.trace_id, 'supervisor.rejected',
                    {'plan_id':plan.plan_id,'reason':'coordination_or_plan_validation_failed'}, actor=plan.actor_id)
                response = self._response(plan, '支付分析暂时不可用，未创建可执行转账，请稍后重新核对。', blocked=True)
                response['ok'] = False
                return response
            budget = plan.preflight.get("checks", {}).get("budget", {})
            if budget.get("status") == "missing":
                options = plan.preflight.get("budget_options", [])
                self.audit.write(
                    plan.trace_id,
                    "budget.constraint.blocked",
                    {"plan_id": plan.plan_id, "shortfall_cents": budget.get("evidence", {}).get("shortfall_cents"),
                     "options": options, "reason": "user_choice_required"},
                    actor=plan.actor_id,
                    risk_level=plan.risk_level,
                    intent=plan.intent,
                )
                response = self._response(
                    plan,
                    budget.get("message") or "预算约束未满足，请先选择如何调整。",
                    blocked=True,
                    budget_options=options,
                    budget_decision="user_choice_required",
                )
                response["ok"] = False
                return response
        if plan.risk_level == "red" and self.is_user_frozen(plan.actor_id):
            message = "检测到审计证据链异常，高风险操作已冻结，请联系人工解锁。"
            self.audit.write(plan.trace_id, "security.freeze.blocked", {"plan_id": plan.plan_id, "user_id": plan.actor_id, "reason": "audit_tamper"}, actor=plan.actor_id, risk_level="red", intent=plan.intent)
            response = self._response(plan, message, blocked=True)
            response["ok"] = False
            return response
        if plan.intent == "transfer" and plan.risk_assessment.get("defer"):
            return self._defer_transfer(plan)
        if plan.intent == "transfer" and (plan.entities.get("account_requires_verification") or plan.entities.get("beneficiary") not in self.bank.beneficiaries):
            return self._transfer_details(plan, "已理解你的转账需求，但尚未核验收款账户。请明确收款人及账户；亲属称呼不等于已核验账户。目前没有扣款，演示仅支持列表中的模拟账户。")
        # 反诈是保护性流程：立即执行“识别、冷静期、通知”，绝不进入“确认后继续转账”。
        if plan.intent == "fraud_guard":
            result = self.execute(plan)
            result_view = present_result(plan, result)
            message = result_view["user_message"] if result.get("status") == "SUCCESS" else "已停止办理，未发生扣款。"
            return self._response(plan, message, result)
        if not plan.requires_confirmation:
            result = self.execute(plan)
            return self._response(plan, "已完成查询。" if result.get("status") == "SUCCESS" else "操作未完成，未发生扣款。", result)
        with self.lock:
            self.pending[plan.plan_id] = plan
        return self._response(plan, f"已生成{RISK_LABELS[plan.risk_level]['label']}操作计划，请确认后继续。")

    def _auth_binding(self, plan: AgentPlan) -> AuthBinding:
        step = next((x for x in plan.steps if x.tool in {"create_transfer", "cancel_subscription", "set_family_allowance"}), plan.steps[-1])
        return AuthBinding(plan.actor_id, plan.device_id, plan.plan_id, step.id, step.tool,
                           int(plan.entities.get("amount_cents") or plan.entities.get("allowance_cents") or 0),
                           str(plan.entities.get("beneficiary") or plan.entities.get("member") or ""))

    def _auth_digest(self, plan: AgentPlan) -> str:
        canonical = json.dumps({"plan_id": plan.plan_id, "intent": plan.intent, "entities": plan.entities,
                                "steps": [asdict(x) for x in plan.steps]}, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def _identity_context(self, plan: AgentPlan, credential_id: str = "") -> AdapterContext:
        identity=self.identity_service.current_identity() if self.identity_service else None
        return AdapterContext(plan.trace_id,plan.actor_id,plan.device_id,credential_id,subject_id=plan.actor_id,
                              authorization_ref=identity['session_id'] if identity else '',operation_digest=self._auth_digest(plan))

    def _auth_payload(self, credential: AuthCredential, include_secret: bool = False) -> Dict[str, Any]:
        binding = asdict(credential.binding)
        payload = {**binding, "credential_id": credential.credential_id,
                   "expires_at": credential.expires_at.isoformat(), "nonce_digest": hashlib.sha256(credential.nonce.encode()).hexdigest(),
                   "used_at": credential.used_at.isoformat() if credential.used_at else None,
                   "operation_digest": credential.operation_digest}
        if include_secret:
            payload["nonce"] = credential.nonce
        return payload

    def _auth_reject(self, plan: AgentPlan, credential: Optional[AuthCredential], code: str, message: str) -> Dict[str, Any]:
        self._publish_risk_event(plan, "auth.failed", code, (credential.credential_id if credential else "none") + ":" + code)
        if code == "AUTH_REPLAY":
            self.audit.write(plan.trace_id, "auth.replay", {"plan_id": plan.plan_id, "credential_id": credential.credential_id if credential else ""}, risk_level=plan.risk_level, intent=plan.intent)
        elif code == "AUTH_BINDING_MISMATCH":
            self.audit.write(plan.trace_id, "auth.invalidated", {"plan_id": plan.plan_id, "credential_id": credential.credential_id if credential else "", "reason": "binding_changed"}, risk_level=plan.risk_level, intent=plan.intent)
        return {"ok": False, "error_code": code, "message": message, "trace_id": plan.trace_id}

    def _credential_for(self, plan: AgentPlan, credential_id: str = "") -> Optional[AuthCredential]:
        selected = credential_id or self.authenticated_credential.get(plan.plan_id, "")
        if selected:
            return self.auth_credentials.get(selected)
        return self.auth_credentials.latest_for(plan.plan_id)

    def _check_credential(self, plan: AgentPlan, credential_id: str = "") -> Tuple[Optional[AuthCredential], Optional[Dict[str, Any]]]:
        credential = self._credential_for(plan, credential_id)
        if credential is None:
            return None, {"ok": False, "error_code": "AUTH_REQUIRED", "message": "请先完成强验证。", "trace_id": plan.trace_id}
        status = self.auth_credentials.check(credential, self._auth_binding(plan), self._auth_digest(plan))
        if status=='OK' and self.identity_service:
            with self.identity_service.lock:
                status=self.identity_service.check(self._identity_context(plan),credential.credential_id,self._auth_digest(plan))
        messages = {"AUTH_REPLAY": "认证凭证已使用，拒绝重复提交。", "AUTH_EXPIRED": "认证已超时，请重新认证。",
                    "AUTH_LOCKED": "验证码错误次数过多，本任务已锁定。", "AUTH_BINDING_MISMATCH": "金额、收款人或操作内容已变化，请重新认证。",
                    "AUTH_INVALIDATED": "认证凭证已失效，请重新认证。"}
        if status != "OK":
            return credential, self._auth_reject(plan, credential, status, messages.get(status, "认证凭证无效。"))
        return credential, None

    def _new_challenge(self, plan: AgentPlan) -> Dict[str, Any]:
        credential = self.auth_credentials.issue(self._auth_binding(plan), self._auth_digest(plan))
        if self.identity_service:
            issued=self.identity_service.begin(self._identity_context(plan,credential.credential_id),self._auth_digest(plan),'totp')
            if not issued.ok:return self._auth_reject(plan,credential,issued.code,issued.message)
        self.auth_plan_snapshots[plan.plan_id] = plan
        self.audit.write(plan.trace_id, "auth.challenged", {"plan_id": plan.plan_id, "credential_id": credential.credential_id,
                          "binding": self._auth_payload(credential), "reason": "strong_auth_required"}, risk_level="red", intent=plan.intent)
        response = self._response(plan, "为保障您的账户安全，请在验证窗口完成身份验证。验证码仅用于本次验证，请勿向他人提供。", needs_auth=True)
        response["auth_challenge"] = {"credential_id": credential.credential_id, "expires_at": credential.expires_at.isoformat(),
                                       "binding": asdict(credential.binding)}
        if self.identity_service:
            response['message']='请在身份验证器中核对下一枚未使用的动态码；通过后仍需再次确认操作。'
            response['auth_challenge']['method']='totp'
        return response

    @audit_action
    @durable_action
    def confirm(self, plan_id: str, action_token: str, credential_id: str = "", device_id: str = "", actor_id: str = "") -> Dict[str, Any]:
        plan, error = self._validate_token(plan_id, action_token)
        if error:
            tombstone = self.auth_credentials.get(credential_id) if credential_id else self.auth_credentials.latest_for(plan_id)
            snapshot = self.auth_plan_snapshots.get(plan_id)
            if tombstone and tombstone.used_at is not None and snapshot is not None:
                return self._auth_reject(snapshot, tombstone, "AUTH_REPLAY", "认证凭证已使用，拒绝重复提交。")
            if error.get("error_code") == "AUTH_BINDING_MISMATCH" and not credential_id:
                with self.lock:
                    plan = self.pending.get(plan_id)
                if plan is not None and plan.requires_strong_auth:
                    credential = self._credential_for(plan)
                    if credential is not None:
                        credential.invalidated_reason = "binding_changed"
                    # The consent card remains valid as a review surface; execution still requires a fresh credential.
                    error = None
            if error:
                return error
        assert plan is not None
        if self.dag_store is not None:
            saved_dag = self.dag_store.get('plan-' + plan_id)
            if saved_dag and saved_dag.status not in {'running', 'waiting_human'}:
                return {'ok': False, 'message': '该任务已暂停或结束，请先检查任务状态。'}
        identity_error = self.family.check_identity(plan, actor_id, device_id, "confirm")
        if identity_error:
            return identity_error
        if plan.intent == "transfer":
            preflight = self._refresh_payment_preflight(plan)
            budget = preflight.get("checks", {}).get("budget", {})
            if budget.get("status") == "missing":
                options = preflight.get("budget_options", [])
                self.audit.write(
                    plan.trace_id,
                    "budget.constraint.blocked",
                    {"plan_id": plan.plan_id, "shortfall_cents": budget.get("evidence", {}).get("shortfall_cents"),
                     "options": options, "reason": "state_changed_before_confirmation"},
                    actor=plan.actor_id,
                    risk_level=plan.risk_level,
                    intent=plan.intent,
                )
                response = self._response(
                    plan,
                    budget.get("message") or "预算约束未满足，请先选择如何调整。",
                    blocked=True,
                    budget_options=options,
                    budget_decision="user_choice_required",
                )
                response["ok"] = False
                return response
        if plan.risk_level == "red" and self.is_user_frozen(plan.actor_id):
            response = self._response(plan, "检测到审计证据链异常，高风险操作已冻结，请联系人工解锁。", blocked=True)
            response["ok"] = False
            return response
        if not plan.entities.get("_family") and actor_id and actor_id != plan.actor_id:
            return self._auth_reject(plan, None, "AUTH_BINDING_MISMATCH", "用户身份已变化，请重新认证。")
        if not plan.entities.get("_family") and device_id and device_id != plan.device_id:
            self.audit.write(plan.trace_id, "device.risk", {"task_id": plan.plan_id, "decision_reason": "设备与绑定凭证不一致"}, actor=plan.actor_id, risk_level="red", intent=plan.intent)
            self._publish_risk_event(plan, "device.risk", "设备与绑定凭证不一致")
            return self._auth_reject(plan, None, "AUTH_BINDING_MISMATCH", "设备已变化，请重新认证。")
        live_policy = self.policy.classify(plan.intent, plan.entities, plan.actor_id)
        self._record_policy(plan, live_policy, "confirmation")
        if live_policy.get("authz_allowed") is False:
            return self.family.deny(live_policy["decision_reason"], plan, actor_id)
        if plan.intent == "transfer" and live_policy.get("defer"):
            return self._defer_transfer(plan)
        if live_policy.get("requires_strong_auth"):
            credential = self._credential_for(plan, credential_id)
            if credential is None:
                return self._new_challenge(plan)
            status = self.auth_credentials.check(credential, self._auth_binding(plan), self._auth_digest(plan))
            if status in {"AUTH_EXPIRED", "AUTH_INVALIDATED"} and not credential_id:
                return self._new_challenge(plan)
            if status != "OK":
                return self._auth_reject(plan, credential, status, "认证凭证已失效，请重新认证。")
            if not credential.passed:
                return self._new_challenge(plan)
            credential, auth_error = self._check_credential(plan, credential.credential_id)
            if auth_error:
                return auth_error
            assert credential is not None
            if self.identity_service:
                consumed=self.identity_service.consume(self._identity_context(plan),credential.credential_id,self._auth_digest(plan))
                if not consumed.ok:return self._auth_reject(plan,credential,consumed.code,consumed.message)
            status = self.auth_credentials.consume(credential)
            if status != "OK":
                return self._auth_reject(plan, credential, status, "认证凭证已失效，拒绝执行。")
            self.audit.write(plan.trace_id, "auth.consumed", self._auth_payload(credential), risk_level=plan.risk_level, intent=plan.intent)
            with self.lock:
                self.authenticated.add(plan.plan_id)
                self.authenticated_credential[plan.plan_id] = credential.credential_id
                self._execution_authorized = getattr(self, "_execution_authorized", set())
                self._execution_authorized.add(plan.plan_id)
        with self.lock:
            if plan_id not in self.pending or plan.consent_token in self.consumed_tokens or plan_id in self.executing:
                self.audit.write(plan.trace_id, "consent.replay", {"plan_id": plan_id, "reason": "already_consumed_or_in_progress"}, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
                return {"ok": False, "message": "这张确认卡片已经使用过或正在处理中。"}
            self.executing.add(plan_id)
            self.consumed_tokens.add(plan.consent_token)
            self.consumed_confirmations[plan_id] = {
                "trace_id": plan.trace_id, "actor_id": plan.actor_id, "risk_level": plan.risk_level,
                "intent": plan.intent, "token_digest": hashlib.sha256(plan.consent_token.encode()).hexdigest(),
            }
        self.audit.write(plan.trace_id, "consent.granted", {"plan_id": plan_id, "token_bound": True, "one_time_consumed": True}, risk_level=plan.risk_level, intent=plan.intent)
        # Durable one-way gate before any side effect; recovery only queries, never retries.
        self.save_task_checkpoint(plan, "processing")
        result = self.execute(plan)
        if result.get("status") == "DEFERRED":
            return self._response(plan, result["error"], result, blocked=True)
        if result.get("status") == "PROCESSING":
            message = "银行还在确认这笔交易，请不要重复操作"
        elif result.get("status") == "UNKNOWN":
            message = "结果未知，正在核验这笔交易，请不要重复操作"
        elif result.get('status')=='PAUSED':
            message='任务已暂停；已完成操作以逐项回执和银行记录为准，剩余操作需重新确认。'
        elif result.get("status") == "SUCCESS" and plan.intent == "card_manage":
            message = present_result(plan, result).get("user_message") or "卡片操作已完成。"
        else:
            message = "操作已完成。" if result.get("status") == "SUCCESS" else f"操作未完成，未发生扣款：{result.get('error') or '请稍后重试或联系人工。'}"
        return self._response(plan, message, result)

    @audit_action
    @durable_action
    def verify(self, plan_id: str, action_token: str, code: str, credential_id: str = "", device_id: str = "", actor_id: str = "") -> Dict[str, Any]:
        plan, error = self._validate_token(plan_id, action_token)
        if error:
            tombstone = self.auth_credentials.get(credential_id) if credential_id else self.auth_credentials.latest_for(plan_id)
            snapshot = self.auth_plan_snapshots.get(plan_id)
            if tombstone and tombstone.used_at is not None and snapshot is not None:
                return self._auth_reject(snapshot, tombstone, "AUTH_REPLAY", "认证凭证已使用，拒绝重复提交。")
            return error
        assert plan is not None
        identity_error = self.family.check_identity(plan, actor_id, device_id, "verify")
        if identity_error:
            return identity_error
        if not plan.entities.get("_family") and actor_id and actor_id != plan.actor_id:
            return self._auth_reject(plan, None, "AUTH_BINDING_MISMATCH", "用户身份已变化，请重新认证。")
        if not plan.entities.get("_family") and device_id and device_id != plan.device_id:
            self.audit.write(plan.trace_id, "device.risk", {"task_id": plan.plan_id, "decision_reason": "设备与绑定凭证不一致"}, actor=plan.actor_id, risk_level="red", intent=plan.intent)
            self._publish_risk_event(plan, "device.risk", "设备与绑定凭证不一致")
            return self._auth_reject(plan, None, "AUTH_BINDING_MISMATCH", "设备已变化，请重新认证。")
        credential = self._credential_for(plan, credential_id)
        if credential is None:
            if self.identity_service:return self._new_challenge(plan)
            credential = self.auth_credentials.issue(self._auth_binding(plan), self._auth_digest(plan))
            self.audit.write(plan.trace_id, "auth.challenged", {"plan_id": plan.plan_id, "credential_id": credential.credential_id,
                              "binding": self._auth_payload(credential), "reason": "verify_without_prior_challenge"}, risk_level="red", intent=plan.intent)
        credential, auth_error = self._check_credential(plan, credential.credential_id)
        if auth_error:
            return auth_error
        assert credential is not None
        factor_ok=(self.identity_service.verify(self._identity_context(plan),credential.credential_id,str(code),self._auth_digest(plan)).ok
                   if self.identity_service else hmac.compare_digest(str(code), POLICY["demo_otp"]))
        if not factor_ok:
            attempts = self.auth_credentials.fail_attempt(credential)
            self.breaker.failure()
            self.audit.write(plan.trace_id, "auth.failed", {"plan_id": plan_id, "credential_id": credential.credential_id, "attempts": attempts}, risk_level=plan.risk_level, intent=plan.intent)
            self._publish_risk_event(plan, "auth.failed", "验证码校验失败", credential.credential_id + ":" + str(attempts))
            if attempts >= POLICY["auth_max_attempts"]:
                self.audit.write(plan.trace_id, "auth.locked", {"plan_id": plan_id, "credential_id": credential.credential_id}, risk_level=plan.risk_level, intent=plan.intent)
                return {"ok": False, "error_code": "AUTH_LOCKED", "message": f"验证码错误 {POLICY['auth_max_attempts']} 次，本任务已锁定。", "breaker": self.breaker.state()}
            return {"ok": False, "error_code": "AUTH_FAILED", "message": "动态码未通过，请核对当前未使用的身份验证器动态码。" if self.identity_service else "验证码错误，请核对页面上的演示验证码。", "breaker": self.breaker.state()}
        status = self.auth_credentials.pass_auth(credential)
        if status != "OK":
            return self._auth_reject(plan, credential, status, "认证凭证已失效，拒绝继续。")
        with self.lock:
            self.authenticated.add(plan_id)
            self.authenticated_credential[plan_id] = credential.credential_id
        self.audit.write(plan.trace_id, "auth.passed", {"plan_id": plan_id, "credential_id": credential.credential_id, "binding": self._auth_payload(credential)}, risk_level=plan.risk_level, intent=plan.intent)
        response = self._response(plan, "身份验证已通过。请核对本次办理内容，确认无误后提交。")
        response["credential_id"] = credential.credential_id
        return response

    @audit_action
    @durable_action
    def cancel(self, plan_id: str, action_token: str, actor_id: str = "", device_id: str = "") -> Dict[str, Any]:
        plan, error = self._validate_token(plan_id, action_token)
        if error:
            return error
        assert plan is not None
        identity_error = self.family.check_identity(plan, actor_id, device_id, "cancel")
        if identity_error:
            return identity_error
        with self.lock:
            if plan_id not in self.pending or plan_id in self.executing or plan.consent_token in self.consumed_tokens:
                return {"ok": False, "message": "这张确认卡片已经使用过或正在处理中。"}
            if self.dag_store is not None:
                controlled = self.dag_control('cancel', 'plan-' + plan_id, plan.actor_id, plan.device_id)
                if not controlled.get('ok'):
                    return controlled
            self.pending.pop(plan_id, None)
            self.authenticated.discard(plan_id)
            self.consumed_tokens.add(plan.consent_token)
        self.audit.write(plan.trace_id, "plan.cancelled", {"plan_id": plan_id, "token_invalidated": True}, risk_level=plan.risk_level, intent=plan.intent)
        return {"ok": True, "message": "已取消剩余操作；已完成的操作以银行记录为准。" if self.dag_store is not None else "已取消本次操作，未发生扣款。"}

    def _dag_adopted(self, task_id):
        with self.audit.lock:
            if not self.audit.db.execute("SELECT 1 FROM sqlite_master WHERE name='task_dags'").fetchone():
                return False
            row = self.audit.db.execute('SELECT state_json FROM task_dags WHERE dag_id=?', ('linkage-' + task_id,)).fetchone()
        return bool(row and json.loads(row[0]).get('metadata', {}).get('execution_owner') == 'dag')

    def dynamic_dag(self,action,plan_id,actor_id,device_id,*,proposal=None,action_token='',mode='offline',model_consent=False):
        from dag_planner import generate,DagProposalError,topology_digest
        if self.dag_store is None:return {'ok':False,'error_code':'DAG_DISABLED','message':'DAG功能已关闭。'}
        if action not in {'propose','adopt'}:return {'ok':False,'error_code':'INVALID_DAG_ACTION'}
        with self.recovery_task_lock(plan_id):
            if self.recovery_failed:return self.recovery_unavailable()
            plan=self.pending.get(plan_id)
            if plan is None:return {'ok':False,'error_code':'PLAN_NOT_PENDING','message':'请先生成尚未执行的业务确认计划。'}
            if actor_id!=plan.actor_id or device_id!=plan.device_id:
                return self.family.deny('动态任务图仅由原计划本人及绑定设备控制',plan,actor=actor_id,device=device_id)
            if not plan.requires_confirmation or any(s.status!='pending' for s in plan.steps):
                return {'ok':False,'error_code':'PLAN_ALREADY_STARTED','message':'已开始的计划不能重新编排。'}
            existing=self.dag_store.get('plan-'+plan_id)
            if existing and (existing.status not in {'running','waiting_human'} or any(n.node_type=='action' and n.status!='pending' for n in existing.nodes)):
                return {'ok':False,'error_code':'DAG_ALREADY_STARTED','message':'已开始或结束的图不能重新编排。'}
            if self.breaker.is_locked() or self.is_user_frozen(plan.actor_id):
                return {'ok':False,'error_code':'SECURITY_FROZEN','message':'安全状态不允许重新编排。'}
            allowed_reads=[name for name in ('get_balance','list_transactions','analyze_bills')
                if self.policy.classify_tool(plan.intent,plan.entities,plan.actor_id,name).get('authz_allowed') is not False]
            try:
                source='user_structured'
                if proposal is None:
                    if mode=='model' and model_consent is not True:raise DagProposalError('model planning requires explicit public-catalog consent')
                    proposal,source=generate(plan,TOOL_REGISTRY,allowed_reads,mode=mode,
                        client=self.parser.llm.client if mode=='model' else None)
                reviewed=self.supervisor.review_dynamic_dag(plan,proposal,TOOL_REGISTRY,allowed_reads)
                digest=topology_digest(reviewed.dag)
                labels={name:title for name,title,_ in reviewed.steps}
                def view_nodes():
                    return [{'node_id':n.node_id,'node_type':n.node_type,'tool':n.tool,'depends_on':n.depends_on[:],'status':n.status,
                             'label':labels.get(n.node_id,'等待指定时间' if n.node_type=='delay' else '需要你确认'),
                             **({'run_at':n.params['run_at']} if n.node_type=='delay' else {})} for n in reviewed.dag.nodes]
                public_nodes=view_nodes()
                if action=='propose':
                    self.audit.write(plan.trace_id,'dag.proposal.reviewed',{'plan_id':plan_id,'source':source,
                        'budget':reviewed.budget,'execution_authority':False},actor=plan.actor_id)
                    return {'ok':True,'proposal':copy.deepcopy(proposal),'proposal_digest':digest,'nodes':public_nodes,
                            'budget':reviewed.budget,'source':source,'business_executed':False,'requires_adoption':True}
                _,error=self._validate_token(plan_id,action_token)
                if error:return error
                # All validation precedes mutation. Only platform-created steps and
                # original trusted entities are retained; client parameters are absent.
                updated=copy.deepcopy(plan)
                updated.steps=[PlanStep(name,title,tool) for name,title,tool in reviewed.steps]
                updated.entities['_dynamic_dag_sha256']=digest
                updated.consent_token=secrets.token_urlsafe(32)
                updated.token_expires_at=(datetime.now(timezone.utc)+timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat()
                updated.entity_digest=self._auth_digest(updated)
                self.dag_store.save(reviewed.dag,{'event_type':'dag.dynamic.adopted','source':source,'budget':reviewed.budget})
                DagExecutor(reviewed.dag,lambda *a: (_ for _ in ()).throw(ValueError('no authority before new confirmation')),self.dag_store.checkpoint).run()
                old_credential=self._credential_for(plan)
                if old_credential is not None:old_credential.invalidated_reason='dag_replanned'
                self.save_task_checkpoint(updated,'waiting_confirmation')
                with self.lock:
                    self.consumed_tokens.add(plan.consent_token);self.pending[plan_id]=updated
                    self.auth_plan_snapshots[plan_id]=updated
                    self.authenticated.discard(plan_id);self.authenticated_credential.pop(plan_id,None)
                return self._response(updated,'动态任务图已采纳，旧确认卡已失效。请核对图与原业务内容，再按新确认卡办理。',
                    dag_id=reviewed.dag.dag_id,dynamic_graph=view_nodes(),proposal_digest=digest,budget=reviewed.budget,business_executed=False)
            except (DagProposalError,SupervisorError,ValueError,TypeError) as exc:
                self.audit.write(plan.trace_id,'dag.proposal.rejected',{'plan_id':plan_id,'reason':type(exc).__name__},actor=plan.actor_id)
                return {'ok':False,'error_code':'DAG_PROPOSAL_REJECTED','message':'任务图未通过结构、权限或预算检查，原计划未改变。'}
            except sqlite3.Error:
                self.recovery_failed=True
                return self.recovery_unavailable()

    def ensure_plan_dag(self, plan):
        if self.dag_store is None:
            return None
        dag = self.dag_store.get('plan-' + plan.plan_id)
        if dag is None:
            dag = plan_to_dag(plan)
            dag.metadata['execution_owner'] = 'dag'
            self.dag_store.save(dag, {'event_type': 'dag.created'})
            if dag.nodes[0].node_type == 'human_gate':
                DagExecutor(dag, lambda *a: (_ for _ in ()).throw(ValueError('no dispatch before consent')),
                            self.dag_store.checkpoint).run()
        return dag

    def voice_deletion_request(self,trace_id,actor_id,device_id):
        """Prepare a provider support draft; never send or claim cloud erasure."""
        if not isinstance(trace_id,str) or not 0<len(trace_id)<=128:
            return {'ok':False,'error_code':'INVALID_VOICE_TRACE','message':'请先选择本人的语音处理记录。'}
        events=self.audit.list_for_trace(trace_id)
        voice=next((e for e in reversed(events) if e['event_type'] in {'voice.transcribed','voice.rejected','voice.evidenced'}),None)
        if voice is None:return {'ok':False,'error_code':'VOICE_EVIDENCE_NOT_FOUND','message':'未找到该语音处理记录。'}
        denied=self.family.trace_guard({'user_id':actor_id,'device_id':device_id},trace_id)
        if denied:return denied
        payload=voice['payload']
        if payload.get('device_id')!=device_id:return {'ok':False,'error_code':'DEVICE_MISMATCH','message':'请使用原设备会话。'}
        if not self.audit.verify_trace(trace_id)['valid']:
            return {'ok':False,'error_code':'AUDIT_INTEGRITY_FAILED','message':'来源审计未通过核验。'}
        if payload.get('outbound_attempted') is not True:
            return {'ok':True,'status':'LOCAL_ONLY_NO_PROVIDER_UPLOAD','delivery':'NOT_SENT','deletion_verified':False,
                    'message':'该记录未尝试云端识别，无需创建供应商录音删除申请。'}
        receipt=payload.get('provider_receipt') or {};request_id=receipt.get('provider_request_id')
        if not isinstance(request_id,str) or not re.fullmatch(r'asr_[0-9a-f]{32}',request_id):
            return {'ok':False,'error_code':'PROVIDER_REQUEST_ID_UNAVAILABLE','message':'该历史记录缺少供应商请求追踪号，不能伪造删除回执。'}
        e=payload.get('audio_evidence') or {}
        draft=('请核查以下ASR请求的业务音频是否按贵司政策完成处理与销毁；如存在保留副本，请处理删除申请并回复处理范围和结果。\n'
               '此申请不涉及自定义音色删除。\n请求追踪号：'+request_id+'\n请求时间：'+voice['timestamp']+
               '\n标准WAV SHA256：'+str(e.get('normalized_wav_sha256',''))+
               '\n本申请未附录音、转写全文、账户信息或API密钥。')
        self.audit.write(trace_id,'voice.deletion_draft.created',voice_audit_evidence({
            'provider_request_id':request_id,'device_id':device_id,'delivery':'NOT_SENT','deletion_verified':False}),actor=actor_id)
        return {'ok':True,'status':'MANUAL_REQUEST_DRAFT','delivery':'NOT_SENT','deletion_verified':False,
                'recipient':'contact@siliconflow.cn','provider_request_id':request_id,'draft':draft,
                'message':'仅生成申请草稿，请自行核对后联系供应商；尚未发送，也没有删除回执。'}

    def _finish_execution(self, plan):
        with self.lock:
            self.pending.pop(plan.plan_id, None)
            self.authenticated.discard(plan.plan_id)
            self.authenticated_credential.pop(plan.plan_id, None)
            getattr(self, '_execution_authorized', set()).discard(plan.plan_id)
            self.executing.discard(plan.plan_id)

    def execute(self, plan):
        if self.dag_store is None:
            return self._execute_steps(plan)
        dag = self.ensure_plan_dag(plan)
        if dag.metadata.get('source')=='dynamic_plan':
            from dag_planner import topology_digest
            if not hmac.compare_digest(str(plan.entities.get('_dynamic_dag_sha256','')),topology_digest(dag)):
                self.audit.write(plan.trace_id,'dag.binding.rejected',{'plan_id':plan.plan_id},actor=plan.actor_id)
                self._finish_execution(plan)
                return {'ok':False,'status':'FAILED','error':'任务图绑定已变化，拒绝执行，请重新核对。','trace_id':plan.trace_id}
        if dag.status == 'completed':
            # Trusted internal replay reads stored/query evidence; never dispatch.
            results = copy.deepcopy([r for n in dag.nodes for r in n.result.get('execution', {}).get('output', {}).get('results', [])])
            status = 'SUCCESS'
            for item in results:
                if item['tool'] == 'create_transfer':
                    query = self.bank.query_transaction(item['data']['transaction_id'])
                    item['data'] = dict(query.data, idempotent_replay=True)
                    if not query.ok or query.data.get('status') in {'failed', 'reversed'}:
                        status = 'FAILED'
                else:
                    item['data']['idempotent_replay'] = True
            return {'ok': status == 'SUCCESS', 'status': status, 'results': results, 'trace_id': plan.trace_id}
        if (dag.status not in {'running', 'waiting_human'} or
                (plan.requires_strong_auth and plan.plan_id not in getattr(self, '_execution_authorized', set()))):
            return {'ok': False, 'status': 'FAILED', 'error': '任务状态或认证不允许执行，请重新核实。', 'trace_id': plan.trace_id}
        outputs = []
        def dispatch(node, tool, params, key):
            step = next(s for s in plan.steps if s.id == node.node_id)
            token = _DAG_TOOL_KEY.set(key)
            try:
                try:
                    out = self._execute_steps(plan, [step], finalize=False)
                except (TimeoutError, ConnectionError, OSError):
                    # A transport exception cannot prove whether the core
                    # accepted the idempotent request.  Surface a stable
                    # UNKNOWN receipt; DagExecutor keeps the node running
                    # for reconciliation and never retries the side effect.
                    self._unknown_outcome_origins[plan.plan_id] = self.bank.fault_injection or "submit_exception"
                    out = {"ok": True, "status": "UNKNOWN",
                           "steps": [asdict(s) for s in plan.steps], "results": [],
                           "trace_id": plan.trace_id}
            finally:
                _DAG_TOOL_KEY.reset(token)
            outputs.append(out)
            with self.lock:
                pause_requested = dag.dag_id in self._dag_pause_requests
                self._dag_pause_requests.discard(dag.dag_id)
            if pause_requested and dag.status == 'running':
                executor.pause()
            # Preserve the full tool evidence, but do not interpret domain strings
            # (LOCKED/CANCELLED/ACTIVE) as bank transaction states.
            return {'ok': out['ok'], 'status': 'processing' if out['status'] in {'PROCESSING', 'UNKNOWN'}
                    else 'succeeded' if out['status'] == 'SUCCESS' else 'failed',
                    'output': out}
        executor = DagExecutor(dag, dispatch, self.dag_store.checkpoint)
        self.audit.write(plan.trace_id, 'task.started', {'plan_id': plan.plan_id, 'dag_id': dag.dag_id}, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
        try:
            if dag.status == 'waiting_human':
                gate = next((n for n in dag.nodes if n.node_type == 'human_gate' and n.status == 'running'), None)
                if gate is None:
                    return {'ok': False, 'status': 'PROCESSING', 'error': '结果待核实，请勿重复操作。', 'trace_id': plan.trace_id}
                executor.approve(gate.node_id)
            executor.run()
            if dag.metadata.get('source')=='dynamic_plan' and dag.status=='running':
                # Delays never schedule a future financial action without renewed
                # consent. Persist a pause; resume issues a fresh confirmation.
                executor.pause()
            receipt = dag_receipt(dag)
            for failed_output in (out for out in outputs if out['status'] not in {'SUCCESS', 'PROCESSING', 'UNKNOWN'}):
                # Keep the tool result, but attach the durable DAG snapshot so the
                # user sees completed, failed, and skipped nodes separately.
                out = dict(failed_output, receipt=receipt, dag_id=dag.dag_id)
                return out
            results = [r for n in dag.nodes for r in n.result.get('execution', {}).get('output', {}).get('results', [])]
            has_unknown = any(out['status'] == 'UNKNOWN' for out in outputs)
            status = 'UNKNOWN' if has_unknown else ('SUCCESS' if dag.status == 'completed' else 'PROCESSING' if dag.status == 'waiting_human' else 'PAUSED' if dag.status == 'paused' else 'FAILED')
            out = {'ok': status != 'FAILED', 'status': status, 'steps': [asdict(s) for s in plan.steps],
                   'results': results, 'trace_id': plan.trace_id, 'dag_id': dag.dag_id}
            if receipt.get('status') in {'UNKNOWN', 'PARTIAL', 'FAILED', 'PROCESSING'}:
                out['receipt'] = receipt
            if plan.intent == 'fraud_guard':
                out['task_status'] = 'cooling_off'
            self.audit.write(plan.trace_id, 'task.settled', {'plan_id': plan.plan_id, 'dag_id': dag.dag_id, 'status': status, 'results': results}, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
            return out
        finally:
            self._finish_execution(plan)

    def _birthday_executor(self, dag, now=None):
        def dispatch(node, tool, params, key):
            task = linkage_view(dag)
            plan = AgentPlan(dag.plan_id, dag.trace_id, 'birthday_linkage', '生日联动', 'yellow', True, False,
                             dict(params, birthday_date=task['birthday_date']), [], now_iso(),
                             actor_id=dag.actor_id, device_id=dag.metadata['device_id'])
            token = _DAG_TOOL_KEY.set(key)
            try:
                result = self._execute_step(plan, PlanStep(node.node_id, '生日任务节点', tool))
            finally:
                _DAG_TOOL_KEY.reset(token)
            event = 'linkage.compensation.completed' if tool == 'compensate_subaccount_lock' else 'linkage.node.completed'
            self.audit.write(dag.trace_id, event if result.ok else 'linkage.node.failed',
                             {'task_id': task['task_id'], 'node_id': node.node_id, 'tool_result': result.data},
                             actor=dag.actor_id, intent=dag.intent)
            with self.lock:
                pause_requested = dag.dag_id in self._dag_pause_requests
                self._dag_pause_requests.discard(dag.dag_id)
            if pause_requested:
                # Pause at the next persisted node boundary, not in a bank call.
                dag.metadata['pause_after_node'] = True
            return {'ok': result.ok, 'tool_result': asdict(result)}
        def checkpoint(snapshot, event):
            self.dag_store.checkpoint(snapshot, event)
            if (event['event_type'] == 'dag.node.transition' and event.get('to') == 'succeeded'
                    and dag.metadata.pop('pause_after_node', False) and dag.status == 'running'):
                dag.transition('paused')
                self.dag_store.save(dag, {'event_type': 'dag.transition', 'from': 'running', 'to': 'paused'})
        return DagExecutor(dag, dispatch, checkpoint,
                           clock=(lambda: datetime.fromisoformat(now)) if now else None)

    def _run_birthday_dag(self, dag, now=None):
        executor = self._birthday_executor(dag, now)
        if dag.status in {'running', 'waiting_human'}:
            for node in dag.nodes:
                if node.status == 'running' and node.node_type == 'action':
                    result = self.bank.workflow_result(node.tool, dag.metadata['legacy_state']['task_id'])
                    if result is not None:
                        executor.reconcile(node.node_id, {'ok': True, 'tool_result': {'ok': True, 'data': result}})
        executor.run()
        return dag

    def advance_dags(self, now=None):
        completed = []
        for saved in self.dag_store.list_recoverable():
            with self.recovery_task_lock(saved.plan_id):
                dag = self.dag_store.get(saved.dag_id)
                if dag.metadata.get('execution_owner') != 'dag':
                    continue
                if dag.metadata.get('source') == 'linkage_tasks':
                    if dag.status in {'running', 'waiting_human'}:
                        self._run_birthday_dag(dag, now)
                    if any(n.node_id in {'B', 'C'} and n.status == 'succeeded' for n in dag.nodes):
                        completed.append(dag.metadata['legacy_state']['task_id'])
                elif dag.status in {'running', 'waiting_human'}:
                    executor = DagExecutor(dag, lambda *a: (_ for _ in ()).throw(ValueError('recovery never retries')), self.dag_store.checkpoint)
                    for node in dag.nodes:
                        if node.status == 'running' and (node.tool == 'create_transfer' or self.bank_service is not None and node.tool in {'report_card_loss','unreport_card_loss'}):
                            with self.bank.lock:
                                row = self.bank._row_for_idempotency(node.idempotency_key)
                            if row:
                                try:
                                    result = self.bank.query_transaction(row['transaction_id'])
                                except (TimeoutError, ConnectionError, OSError):
                                    result = ToolResult(True, 'query_transaction',
                                                        {'status': 'unknown', 'outcome_unknown': True},
                                                        'OUTCOME_UNKNOWN', '银行暂时无法确认这笔交易。')
                                executor.reconcile(node.node_id, {'ok': result.ok, 'data': result.data})
                    if dag.status == 'running':
                        if all(n.status == 'succeeded' for n in dag.nodes):
                            executor._task_transition('completed')
                        else:
                            # No restored authority. Only explicit confirmation or
                            # operator handoff may continue an interrupted task.
                            executor._task_transition('waiting_human')
        return completed

    def _reconfirm_dag(self, dag):
        """Resuming a partially executed plan never restores old credentials."""
        saved = self.recovery_store.get(dag.plan_id)
        if saved and dag.metadata.get('source')=='dynamic_plan':
            from dag_planner import topology_digest
            expected=saved[1]['plan']['entities'].get('_dynamic_dag_sha256','')
            if not hmac.compare_digest(str(expected),topology_digest(dag)):
                return {'ok':False,'error_code':'DAG_BINDING_MISMATCH','message':'任务图绑定不匹配，不能通过恢复重新授权。'}
        dynamic_gate=(dag.metadata.get('source')=='dynamic_plan' and
            any(n.node_type=='human_gate' and n.status=='running' for n in dag.nodes) and
            not any(n.node_type!='human_gate' and n.status=='running' for n in dag.nodes))
        if not saved or (any(n.status == 'running' for n in dag.nodes) and not dynamic_gate):
            DagExecutor(dag, lambda *a: None, self.dag_store.checkpoint)._task_transition('waiting_human')
            return {'ok': False, 'error_code': 'RECONCILIATION_REQUIRED', 'message': '存在结果不明的节点，请先查询或转人工核实。'}
        data = dict(saved[1]['plan'])
        data['steps'] = [PlanStep(**step) for step in data['steps']]
        plan = AgentPlan(**data)
        if not dynamic_gate:
            gate_id = 'resume-confirmation-' + str(len(dag.nodes))
            succeeded = [n.node_id for n in dag.nodes if n.status == 'succeeded']
            for node in dag.nodes:
                if node.status in {'pending', 'ready'}:node.depends_on.append(gate_id)
            dag.nodes.append(DagNode(gate_id,node_type='human_gate',depends_on=succeeded,
                                     idempotency_key='idem:'+dag.dag_id+':'+gate_id))
            dag.topological_order()
            self.dag_store.save(dag,{'event_type':'dag.confirmation.renewed'})
        # Ready action nodes cannot run while the new gate is unsatisfied. Drive
        # the gate itself, leaving the existing action states intact.
        executor = DagExecutor(dag, lambda *a: None, self.dag_store.checkpoint)
        if not dynamic_gate:
            gate = dag.nodes[-1]
            executor._node_transition(gate,'ready');executor._node_transition(gate,'running')
            executor._task_transition('waiting_human')
        if dag.metadata.get('source')=='dynamic_plan':
            from dag_planner import topology_digest
            plan.entities['_dynamic_dag_sha256']=topology_digest(dag)
        plan.consent_token = secrets.token_urlsafe(32)
        plan.token_expires_at = (datetime.now(timezone.utc) + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat()
        canonical = json.dumps({'plan_id': plan.plan_id, 'intent': plan.intent, 'entities': plan.entities,
                                'steps': [asdict(x) for x in plan.steps]}, ensure_ascii=False, sort_keys=True, separators=(',', ':'))
        plan.entity_digest = hashlib.sha256(canonical.encode()).hexdigest()
        live = self.policy.classify(plan.intent, plan.entities, plan.actor_id)
        self._record_policy(plan, live, 'dag_resume')
        self.pending[plan.plan_id] = plan
        self.auth_plan_snapshots[plan.plan_id] = plan
        response = self._new_challenge(plan) if plan.requires_strong_auth else self._response(plan, '恢复前请重新核对剩余操作并确认，已完成节点不会重做。')
        self.save_task_checkpoint(plan, 'waiting_confirmation')
        return response

    def dag_control(self, action, dag_id, actor_id, device_id, action_token='', credential_id='', decision='approve'):
        if self.dag_store is None:
            return {'ok': False, 'error_code': 'DAG_DISABLED', 'message': 'DAG 功能已关闭。'}
        dag = self.dag_store.get(dag_id)
        if dag is None:
            return {'ok': False, 'error_code': 'DAG_NOT_FOUND', 'message': '任务图不存在。'}
        if actor_id != dag.actor_id or device_id != dag.metadata.get('device_id'):
            return self.family.deny('当前会话不能操作其他用户或设备的任务图', actor=actor_id, subject=dag.actor_id, device=device_id)
        guard = self.recovery_task_lock(dag.plan_id)
        if action == 'pause' and not guard.acquire(blocking=False):
            with self.lock:
                self._dag_pause_requests.add(dag_id)
            return {'ok': True, 'dag_id': dag_id, 'status': 'pause_requested',
                    'message': '已请求在当前工具返回后暂停；不会中断银行已受理的操作。'}
        elif action == 'pause':
            guard.release()
        with guard:
            dag = self.dag_store.get(dag_id)
            if action == 'state':
                # No internal metadata, credentials, raw tool evidence or PII exposed.
                return {'ok': True, 'dag_id': dag_id, 'plan_id': dag.plan_id, 'trace_id': dag.trace_id,
                        'status': dag.status, 'nodes': [{'node_id': n.node_id, 'node_type': n.node_type,
                        'tool': n.tool, 'depends_on': n.depends_on, 'status': n.status} for n in dag.nodes]}
            birthday = dag.metadata.get('source') == 'linkage_tasks'
            executor = self._birthday_executor(dag) if birthday else DagExecutor(dag, lambda *a: (_ for _ in ()).throw(ValueError('no execution authority')), self.dag_store.checkpoint)
            handoff_card = None
            try:
                if action == 'approve':
                    if decision == 'handoff':
                        unknown = any(n.status == 'running' and n.node_type == 'action' and
                                      (n.tool == 'create_transfer' or n.result.get('execution', {}).get('output', {}).get('status') in {'UNKNOWN', 'PROCESSING'})
                                      for n in dag.nodes)
                        executor.handoff()
                        self.pending.pop(dag.plan_id, None)
                        self.audit.write(dag.trace_id, 'dag.handoff.requested', {'task_id': dag.plan_id,
                                         'decision_reason': '仅登记人工接管请求，未接通真人'}, actor=actor_id)
                        try:
                            handoff_card = self._create_handoff_card(
                                'unknown_result' if unknown else 'user_requested', dag=dag,
                                actor_id=actor_id, device_id=device_id, trace_id=dag.trace_id,
                                pending_items=['人工核验银行原请求结果；演示系统不自动重发副作用'])
                        except Exception:
                            handoff_card = None
                    elif decision == 'reject':
                        gate = next(n for n in dag.nodes if n.node_type == 'human_gate' and n.status == 'running')
                        executor.approve(gate.node_id, False)
                        self.pending.pop(dag.plan_id, None)
                        try:
                            handoff_card = self._create_handoff_card(
                                'human_gate_rejected', dag=dag, actor_id=actor_id,
                                device_id=device_id, trace_id=dag.trace_id,
                                pending_items=['用户拒绝当前确认；如需继续请重新生成并核对计划'])
                        except Exception:
                            handoff_card = None
                    elif decision == 'approve' and not birthday and dag.status == 'waiting_human':
                        if dag.metadata.get('source')=='dynamic_plan' and dag.plan_id not in self.pending:
                            return self._reconfirm_dag(dag)
                        if not any(n.status == 'running' for n in dag.nodes):
                            executor._task_transition('running')
                            return self._reconfirm_dag(dag)
                        return self.confirm(dag.plan_id, action_token, credential_id, device_id, actor_id)
                    else:
                        raise ValueError('approval cannot settle an ambiguous bank result')
                elif action == 'pause':
                    executor.pause()
                elif action == 'resume':
                    executor.resume()
                    if birthday:
                        self._run_birthday_dag(dag)
                    else:
                        return self._reconfirm_dag(dag)
                elif action == 'cancel':
                    if dag.status == 'waiting_human':
                        gates = [n for n in dag.nodes if n.status == 'running']
                        if not gates or any(n.node_type != 'human_gate' for n in gates):
                            raise ValueError('cannot cancel an unresolved effect')
                        for gate in gates:
                            gate.result['execution'] = {'ok': False, 'data': {'cancelled': True}}
                            executor._node_transition(gate, 'failed')
                        executor._task_transition('running')
                    if dag.status != 'cancelled':
                        executor.cancel()
                    self.pending.pop(dag.plan_id, None)
                else:
                    raise ValueError('unknown control')
            except (ValueError, StopIteration):
                return {'ok': False, 'error_code': 'DAG_INVALID_STATE', 'message': '当前任务状态不允许此操作；结果不明时请转人工核实。'}
            if not birthday and dag.status in {'handoff', 'failed', 'cancelled'}:
                saved = self.recovery_store.get(dag.plan_id)
                if saved:
                    self.recovery_store.save(dag.plan_id, dag.status, saved[1])
            response = {'ok': True, 'dag_id': dag_id, 'status': dag.status,
                        'message': '操作已记录。已下单礼品不自动撤销；仅补偿已声明可回退的预算锁定。' if action == 'cancel' else '任务状态已更新。'}
            if handoff_card is not None:
                response['handoff_card'] = handoff_card
            return response

    def _execute_steps(self, plan: AgentPlan, steps=None, finalize=True) -> Dict[str, Any]:
        if self.breaker.is_locked():
            return {"ok": False, "status": "FAILED", "error": "安全熔断已触发，拒绝继续执行。", "trace_id": plan.trace_id}
        if plan.requires_strong_auth and plan.plan_id not in getattr(self, "_execution_authorized", set()):
            return {"ok": False, "status": "FAILED", "error": "强验证未完成，拒绝直接执行。", "trace_id": plan.trace_id}
        results: List[Dict[str, Any]] = []
        if finalize:
            self.audit.write(plan.trace_id, "task.started", {"plan_id": plan.plan_id}, risk_level=plan.risk_level, intent=plan.intent)
        try:
            for step in (plan.steps if steps is None else steps):
                if step.tool not in TOOL_REGISTRY:
                    self.audit.write(plan.trace_id, "tool.denied", {"tool": step.tool, "reason": "not_registered"}, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
                    step.status = "failed"
                    step.detail = "未注册的工具"
                    return {"ok": False, "status": "FAILED", "steps": [asdict(s) for s in plan.steps], "error": "未注册的工具，已拒绝调用。", "trace_id": plan.trace_id}
                result = self._execute_step(plan, step)
                # create_transfer 返回的只是受理结果；用户可见结论必须以一次只读最终状态查询为准。
                # Transport uncertainty is the exception: do not query synchronously
                # and do not retry the side effect; recovery queries the same idempotency key.
                if step.tool == "create_transfer" and result.data.get("outcome_unknown"):
                    self._unknown_outcome_origins[plan.plan_id] = self.bank.fault_injection or "transport_uncertain"
                    self.audit.write(plan.trace_id, "transaction.unknown", {
                        "transaction_id": result.data.get("transaction_id"),
                        "idempotency_key": _DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:create_transfer",
                        "reason": result.error_code or result.data.get("error_code") or "OUTCOME_UNKNOWN",
                    }, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
                elif step.tool == "create_transfer" and result.data.get("transaction_id"):
                    transaction_id = result.data["transaction_id"]
                    try:
                        queried = self.bank.query_transaction(transaction_id)
                    except (TimeoutError, ConnectionError, OSError):
                        queried = ToolResult(True, "query_transaction", {"status": "unknown", "transaction_id": transaction_id, "outcome_unknown": True}, "OUTCOME_UNKNOWN", "银行暂时无法确认这笔交易。")
                    if queried.ok and queried.data.get("status") == "unknown":
                        result.data = dict(result.data, status="unknown", outcome_unknown=True)
                        self.audit.write(plan.trace_id, "transaction.unknown", {
                            "transaction_id": transaction_id, "idempotency_key": _DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:create_transfer", "reason": queried.error_code or "QUERY_UNAVAILABLE"
                        }, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
                    elif queried.ok:
                        queried_data = dict(queried.data)
                        if result.data.get("idempotent_replay"):
                            queried_data["idempotent_replay"] = True
                        result = ToolResult(
                            queried_data.get("status") != "failed",
                            "create_transfer",
                            queried_data,
                            queried_data.get("error_code") if queried_data.get("status") == "failed" else result.error_code,
                            queried_data.get("error_message") if queried_data.get("status") == "failed" else result.error_message,
                            trace_id=plan.trace_id,
                        )
                        self.audit.write(
                            plan.trace_id,
                            "transaction.status.queried",
                            {"transaction_id": transaction_id, "status": queried_data.get("status"), "state_history": queried_data.get("state_history", [])},
                            actor=plan.actor_id,
                            risk_level=plan.risk_level,
                            intent=plan.intent,
                        )
                    else:
                        # A failed read is not proof of a failed transfer.
                        # Preserve the original tool evidence and mark the
                        # delivery outcome unknown for read-only recovery.
                        self._unknown_outcome_origins[plan.plan_id] = "query_unavailable"
                        result.data = dict(result.data, status="unknown", outcome_unknown=True,
                                           error_code="OUTCOME_UNKNOWN", error_message="银行暂时无法确认这笔交易。")
                        self.audit.write(plan.trace_id, "transaction.unknown", {
                            "transaction_id": transaction_id,
                            "idempotency_key": _DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:create_transfer",
                            "reason": "QUERY_UNAVAILABLE",
                        }, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
                self.audit.write(plan.trace_id, "tool.called", {"step_id": step.id, "tool": step.tool}, risk_level=plan.risk_level, intent=plan.intent)
                if result.error_code == "RISK_DEFERRED":
                    step.status = "deferred"
                    return {"ok": False, "status": "DEFERRED", "error": result.error_message, "trace_id": plan.trace_id}
                if not result.ok:
                    step.status = "failed"
                    step.detail = result.error_message or "执行失败"
                    self.breaker.failure()
                    self.audit.write(plan.trace_id, "tool.failed", {"step": asdict(step), "error_code": result.error_code, "error_message": result.error_message}, risk_level=plan.risk_level, intent=plan.intent)
                    return {"ok": False, "status": "FAILED", "steps": [asdict(s) for s in plan.steps], "error": result.error_message, "trace_id": plan.trace_id}
                if self.bank_service is not None and step.tool in {'report_card_loss','unreport_card_loss'} and result.data.get('bank_request_status') in {'unknown','processing'}:
                    step.detail=json.dumps(result.data,ensure_ascii=False)
                    self.audit.write(plan.trace_id,'card.operation.pending',{'plan_id':plan.plan_id,'status':result.data['bank_request_status']},actor=plan.actor_id)
                    return {'ok':True,'status':'UNKNOWN' if result.data['bank_request_status']=='unknown' else 'PROCESSING',
                            'results':[{'tool':step.tool,'data':result.data,'trace_id':plan.trace_id}],'steps':[asdict(s) for s in plan.steps],'trace_id':plan.trace_id}
                step.status = "completed"
                step.detail = json.dumps(result.data, ensure_ascii=False)
                if step.tool == "create_cooling_off":
                    plan.entities["case_id"] = result.data.get("case_id")
                results.append({"tool": result.tool, "data": result.data, "trace_id": result.trace_id})
                self.audit.write(plan.trace_id, "tool.result", {"step": asdict(step), "tool_result": result.data}, risk_level=plan.risk_level, intent=plan.intent)
                if step.tool in {"report_card_loss", "unreport_card_loss", "verify_card_status"}:
                    event_type = {
                        "report_card_loss": "card.loss.completed",
                        "unreport_card_loss": "card.unloss.completed",
                        "verify_card_status": "card.status.verified",
                    }[step.tool]
                    if result.data.get('no_op'):
                        event_type = 'card.operation.not_needed'
                    self.audit.write(
                        plan.trace_id,
                        event_type,
                        {"plan_id": plan.plan_id, "card_id": result.data.get("card_id"),
                         "status": result.data.get("status"), "idempotent_replay": bool(result.data.get("idempotent_replay"))},
                        actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent,
                    )
                if step.tool == "create_cooling_off":
                    self.audit.write(
                        plan.trace_id,
                        "fraud.alert",
                        {
                            "status": "cooling_off",
                            "case_id": result.data.get("case_id"),
                            "beneficiary": result.data.get("beneficiary"),
                            "amount_cents": result.data.get("amount_cents"),
                            "signals": result.data.get("signals", []),
                            "cooling_off_until": result.data.get("cooling_off_until"),
                        },
                        actor=plan.actor_id,
                        risk_level=plan.risk_level,
                        intent=plan.intent,
                    )
                    self._publish_risk_event(plan, "fraud.blocked", plan.risk_assessment.get("decision_reason", "既有反诈硬规则拦截"))
                    self._publish_risk_event(plan, "transaction.deferred", "反诈保护性冷静期，未发生扣款")
            self.breaker.reset()
            output_status = "SUCCESS"
            if plan.intent == "transfer":
                transfer_result = next((item["data"] for item in reversed(results) if item.get("tool") == "create_transfer"), {})
                if transfer_result.get("outcome_unknown") or transfer_result.get("status") == "unknown":
                    output_status = "UNKNOWN"
                    self._publish_risk_event(plan, "transaction.deferred", "交易结果未知，等待原请求核验", "core_unknown")
                elif transfer_result.get("status") == "processing":
                    output_status = "PROCESSING"
                    self._publish_risk_event(plan, "transaction.deferred", "核心处理中，等待最终状态确认", "core_processing")
            elif plan.intent == "subscription_cancel":
                subscription_result = next((item["data"] for item in reversed(results) if item.get("tool") == "cancel_subscription"), {})
                if subscription_result.get("outcome_unknown") or subscription_result.get("status") == "unknown":
                    output_status = "UNKNOWN"
                    self.audit.write(plan.trace_id, "subscription.unknown", {
                        "subscription_id": subscription_result.get("subscription_id"),
                        "idempotency_key": _DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:cancel_subscription",
                        "reason": "OUTCOME_UNKNOWN",
                    }, actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
            output = {"ok": True, "status": output_status, "steps": [asdict(s) for s in plan.steps], "results": results, "trace_id": plan.trace_id}
            if plan.intent == "fraud_guard":
                output["task_status"] = "cooling_off"
            if finalize:
                self.audit.write(plan.trace_id, "task.settled", {"plan_id": plan.plan_id, "results": results}, risk_level=plan.risk_level, intent=plan.intent)
            return output
        finally:
            if finalize:
                self._finish_execution(plan)

    def _execute_step(self, plan: AgentPlan, step: PlanStep) -> ToolResult:
        if self.bank_service is not None:
            allowed={'get_balance','list_transactions','resolve_beneficiary','check_transfer_constraints','create_transfer',
                     'list_cards','report_card_loss','unreport_card_loss','verify_card_status','clarify','clarify_transfer','human_handoff'}
            if step.tool not in allowed or plan.entities.get('_family'):
                return ToolResult(False,step.tool,error_code='BANK_PROTOCOL_SCOPE',error_message='银行协议演示仅支持本人余额、转账与卡管；其他场景请使用默认离线演示。')
            try:
                ctx=self.bank_service.context(plan.actor_id,device_id=plan.device_id,trace_id=plan.trace_id,
                    operation_digest=self._auth_digest(self.auth_plan_snapshots.get(plan.plan_id,plan)))
                with self.bank_service.scope(ctx):result=self._execute_authorized_step(plan,step)
            except (BankServiceError,ValueError):
                return ToolResult(False,step.tool,error_code='BANK_SERVICE_UNAVAILABLE',error_message='银行协议服务或授权不可核验，已停止。',trace_id=plan.trace_id)
        # The same lock gates trusted grant revocation and the last authorization check.
        # Notification authorization uses the store's separate lock, not this lock.
        elif plan.actor_id in self.bank.guardianships.accounts:
            with self.bank.lock:
                result = self._execute_authorized_step(plan, step)
        else:
            result = self._execute_authorized_step(plan, step)
        # Never mutate the core's cached idempotent result.
        result = copy.copy(result)
        result.trace_id = plan.trace_id
        domain = {'financial_health_report':'finance_health', 'list_subscriptions':'subscription',
                  'list_cards':'card', 'verify_card_status':'card',
                  'list_family_relationships':'family', 'detect_fraud_signals':'antifraud'}.get(step.tool)
        if domain:
            ctx = self._domain_context(plan)
            self._observe_domain(plan, domain, ctx, lambda: tool_snapshot(ctx, domain, result))
        return result

    def _execute_authorized_step(self, plan: AgentPlan, step: PlanStep) -> ToolResult:
        e = plan.entities
        live = self.policy.classify_tool(plan.intent, e, plan.actor_id, step.tool)
        if live.get('authz_allowed') is False:
            denied = self.family.deny(live['decision_reason'], plan)
            return ToolResult(False, step.tool, error_code='AUTHZ_DENIED', error_message=denied['message'])
        if (step.tool in {'report_card_loss', 'unreport_card_loss'} or
                (step.tool == 'cancel_subscription' and e.get('subscription_id'))):
            review = plan.coordination.get('domain_review', {})
            expected_signal = {'report_card_loss':'card_already_lost', 'unreport_card_loss':'card_already_normal'}.get(step.tool)
            if expected_signal and review.get('ready') and expected_signal in review.get('signals', ()):
                # Recheck via a read; never call a write adapter to acknowledge an existing state.
                current = self.bank.card_status_query(e['card_id'], plan.actor_id, plan.trace_id)
                expected_states = {'reported_loss', '挂失'} if step.tool == 'report_card_loss' else {'正常', 'normal', 'active'}
                if current.ok and current.data.get('status') in expected_states:
                    return ToolResult(True, step.tool, dict(current.data, no_op=True), trace_id=plan.trace_id)
                return ToolResult(False, step.tool, error_code='CARD_STATE_CHANGED',
                                  error_message='卡片状态已变化或未能核实，请重新查询后办理。本次未执行写操作。')
            if not review.get('ready') or step.tool not in review.get('tools', ()):
                return ToolResult(False, step.tool, error_code='DOMAIN_REVIEW_REQUIRED',
                                  error_message='本次资料尚不满足办理条件，或无需重复操作。请核实下方说明；本次未执行该操作。')
        if step.tool == 'knowledge_lookup':
            scope=KnowledgeScope(live['knowledge_actor'],live['knowledge_subject'],live['knowledge_delegated'])
            def access_check():
                current=self.policy.classify_tool(plan.intent,e,plan.actor_id,step.tool)
                return (current.get('authz_allowed') is not False and
                        current.get('knowledge_actor')==scope.actor_id and current.get('knowledge_subject')==scope.subject_id and
                        current.get('knowledge_delegated')==scope.delegated)
            data = self.knowledge.answer(e.get('question',''), scope.actor_id,read_scope=scope,access_check=access_check,
                model_client=self.parser.llm.client if self.knowledge_use_model else None)
            if e.get('knowledge_rejection') == 'privacy_blocked':
                data.update(answer='请移除敏感信息后再提出知识问题。',
                            model={'attempted':False,'accepted':False,'reason':'privacy_blocked'})
            self.audit.write(plan.trace_id, 'rag.retrieved', {
                'plan_id':plan.plan_id, 'knowledge_version':data['knowledge_version'], 'status':data['status'],
                'mode':data['mode'], 'citations':[{k:c[k] for k in ('document_id','chunk_id','source_path','line_start','version','source_sha256','content_sha256')}
                                               for c in data['citations']],
                'model':data['model'],'retrieval':data.get('retrieval',{}),
                'reader':scope.actor_id,'subject':scope.subject_id,'delegated':scope.delegated},
                actor=scope.actor_id, risk_level=plan.risk_level, intent=plan.intent)
            return ToolResult(True, step.tool, data)
        if FEATURE_REGISTRY.get(step.tool) is not None:
            return ToolResult(**FEATURE_REGISTRY.execute(self, plan, step, live))
        if step.tool in {'lock_subaccount', 'order_flowers', 'order_cake', 'compensate_subaccount_lock'}:
            if not _DAG_TOOL_KEY.get() or plan.intent != 'birthday_linkage':
                return ToolResult(False, step.tool, error_code='AUTHZ_DENIED', error_message='只能由已授权的生日任务调度。')
            if self.breaker.is_locked() or self.is_user_frozen(plan.actor_id):
                return ToolResult(False, step.tool, error_code='SECURITY_FROZEN', error_message='安全状态不允许继续操作。')
            if step.tool == 'lock_subaccount':
                return self.bank.lock_subaccount(e['amount_cents'], e['task_id'], plan.actor_id)
            if step.tool == 'compensate_subaccount_lock':
                return self.bank.compensate_subaccount_lock(e['task_id'])
            return self.bank.order_linkage_item('flowers' if step.tool == 'order_flowers' else 'cake', e['task_id'], plan.actor_id)
        if step.tool == "get_balance": return self.bank.balance(plan.actor_id)
        if step.tool == "list_transactions": return self.bank.recent_transactions(plan.actor_id)
        if step.tool == "analyze_bills": return self.bank.analyze_bills(plan.actor_id)
        if step.tool == "list_investments": return self.bank.investments_query()
        if step.tool == "list_subscriptions": return self.bank.subscriptions_query()
        if step.tool == "list_cards": return self.bank.cards_query()
        if step.tool == "report_card_loss":
            return self.bank.report_card_loss(
                e.get("card_id", "card_001"), plan.actor_id,
                (_DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:report_card_loss"), plan.trace_id,
            )
        if step.tool == "unreport_card_loss":
            return self.bank.unreport_card_loss(
                e.get("card_id", "card_001"), plan.actor_id,
                (_DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:unreport_card_loss"), plan.trace_id,
            )
        if step.tool == "verify_card_status":
            result = self.bank.card_status_query(e.get("card_id", "card_001"), plan.actor_id, plan.trace_id)
            expected = {'reported_loss', '挂失'} if e.get("card_operation") == "report_loss" else {'正常', 'normal', 'active'}
            if result.ok and result.data.get("status") not in expected:
                return ToolResult(False, step.tool, result.data, error_code="CARD_STATUS_MISMATCH",
                                  error_message="卡片最终状态与本次操作不一致，已停止并保留审计记录。",
                                  trace_id=plan.trace_id)
            return result
        if step.tool == "resolve_beneficiary": return self.bank.resolve_beneficiary(e.get("beneficiary", ""))
        if step.tool == "check_transfer_constraints":
            amount = e.get("amount_cents") or 0
            if plan.actor_id == "demo-user":
                return self.bank.check_transfer_constraints(amount)
            return self.bank.check_transfer_constraints(amount, plan.actor_id)
        if step.tool == "create_birthday_linkage":
            if e.get('schedule_error'):
                return ToolResult(False, step.tool, error_code='INVALID_BIRTHDAY_SCHEDULE', error_message=e['schedule_error'])
            return self.create_birthday_linkage(e.get("birthday_date", ""), plan.actor_id, plan.trace_id, plan.plan_id,
                                               e.get('flowers_days_before', 2), e.get('cake_days_before', 1))
        if step.tool == "create_transfer":
            # 风险检查、额度复检与扣款共用核心锁，防止并发确认跨过风险阈值。
            with self.bank.lock:
                live_policy = self.policy.classify("transfer", e, plan.actor_id)
                self._record_policy(plan, live_policy, "before_side_effect")
                if live_policy.get('authz_allowed') is False:
                    denied = self.family.deny(live_policy['decision_reason'], plan)
                    return ToolResult(False, step.tool, error_code='AUTHZ_DENIED', error_message=denied['message'])
                if live_policy.get("defer"):
                    held = self._defer_transfer(plan)
                    return ToolResult(False, "create_transfer", error_code="RISK_DEFERRED", error_message=held["message"])
                if live_policy.get("requires_strong_auth") and plan.plan_id not in self.authenticated:
                    return ToolResult(False, "create_transfer", error_code="REAUTH_REQUIRED", error_message="当前风险或日累计额度已升级，需要重新完成强验证。")
                amount = e.get("amount_cents") or 0
                if plan.actor_id == "demo-user":
                    recheck = self.bank.check_transfer_constraints(amount)
                else:
                    recheck = self.bank.check_transfer_constraints(amount, plan.actor_id)
                self.audit.write(plan.trace_id, "precondition.rechecked", {"tool": step.tool, "amount_cents": e.get("amount_cents")}, risk_level=plan.risk_level, intent=plan.intent)
                if not recheck.ok:
                    return recheck
                ctx = e.get('_family')
                if ctx:
                    final_policy = self.policy.classify('transfer', e, plan.actor_id)
                    if final_policy.get('authz_allowed') is False:
                        denied = self.family.deny(final_policy['decision_reason'], plan)
                        return ToolResult(False, step.tool, error_code='AUTHZ_DENIED', error_message=denied['message'])
                if ctx and ctx['mode'] == 'delegate' and not self.bank.guardianships.reserve(ctx, plan.plan_id, amount, plan.actor_id):
                    denied = self.family.deny('家庭每日授权额度不足或授权已失效', plan)
                    return ToolResult(False, step.tool, error_code='AUTHZ_DENIED', error_message=denied['message'])
                return self.bank.transfer(e["amount_cents"], e["beneficiary"], e.get("note", ""), (_DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:create_transfer"), plan.actor_id)
        if step.tool == "cancel_subscription":
            if not e.get("subscription_id"):
                code, message = "SUBSCRIPTION_TARGET_REQUIRED", "请说明要取消哪个订阅：视频会员还是云盘会员"
            elif plan.plan_id not in self.executing or plan.consent_token not in self.consumed_tokens:
                code, message = "CONSENT_REQUIRED", "请先核对订阅名称和金额，再确认取消。"
            else:
                return self.bank.cancel_subscription(e["subscription_id"], (_DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:cancel_subscription"))
            self.audit.write(plan.trace_id, "tool.denied", {"plan_id": plan.plan_id, "tool": step.tool, "reason": code},
                             actor=plan.actor_id, risk_level=plan.risk_level, intent=plan.intent)
            return ToolResult(False, step.tool, error_code=code, error_message=message)
        if step.tool == "detect_fraud_signals": return self.bank.detect_fraud_signals(e.get("beneficiary") or "未知收款账户", e.get("amount_cents") or 0, e.get("fraud_signals") or ["需要人工核实"], bool(e.get("is_new_recipient")))
        if step.tool == "create_cooling_off": return self.bank.create_cooling_off(e.get("beneficiary") or "未知收款账户", e.get("amount_cents") or 0, e.get("fraud_signals") or ["需要人工核实"], (_DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:create_cooling_off"))
        if step.tool == "notify_guardian":
            receipts = self.notifications.list_notifications(plan.actor_id, plan.trace_id, "guardian")
            receipt = next((r for r in receipts if r["event_type"] == "fraud.blocked" and r["status"] == "LOCAL_QUEUED"), None)
            return self.bank.notify_guardian(e.get("case_id") or "unknown-case", e.get("beneficiary") or "未知收款账户", e.get("amount_cents") or 0, (_DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:notify_guardian"), plan.actor_id, receipt)
        if step.tool == "financial_health_report": return self.bank.financial_health_report(plan.actor_id)
        if step.tool == "list_savings_goals": return self.bank.list_savings_goals()
        if step.tool == "create_savings_goal": return self.bank.create_savings_goal(e.get("goal_name") or "攒钱目标", e.get("target_cents") or 0, e.get("monthly_cents"), (_DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:create_savings_goal"))
        if step.tool == "idle_cash_check": return self.bank.idle_cash_check()
        if step.tool == "list_family_relationships": return self.bank.list_family_relationships()
        if step.tool == "family_auth_check": return self.bank.family_auth_check(e.get("member") or "", "set_allowance", plan.actor_id, e.get("allowance_cents"))
        if step.tool == "set_family_allowance":
            return self.bank.set_family_allowance(e.get("member") or "", e.get("allowance_cents") or 0, (_DAG_TOOL_KEY.get() or f"idem:{plan.plan_id}:set_family_allowance"), plan.actor_id)
        if step.tool == "calculate_aa_settlement": return self.bank.calculate_aa_settlement(e.get("participants") or [])
        if step.tool == 'human_handoff' and plan.actor_id in self.bank.guardianships.accounts:
            return ToolResult(False, step.tool, error_code='HANDOFF_REQUIRED', error_message='本人认证已完成；请到官方人工渠道办理，演示系统未修改密码或卡片状态。')
        if step.tool in {"clarify", "clarify_transfer", "human_handoff"}:
            return ToolResult(True, step.tool, {"status": "NEEDS_USER_INPUT"})
        return ToolResult(False, step.tool, error_code="UNKNOWN_TOOL", error_message="未注册的工具，已拒绝调用。")

runtime = AgentRuntime(offline=os.getenv("BANK_AGENT_OFFLINE") == "1")
gateway = AgentGateway(runtime)
http_rate_limiter = SlidingWindowRateLimiter()
MAX_BODY_BYTES = POLICY["http_max_body_bytes"]


# ------------------------------- HTTP API --------------------------------
attack_matrix_run_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    server_version = "BankingAgentDemo/1.2"

    def _send(self, status: int, data: Any, content_type: str = "application/json; charset=utf-8"):
        if self.command=='GET' and urlparse(self.path).path=='/api/state' and getattr(runtime,'identity_service',None) and isinstance(data,dict):
            data=dict(data);data.pop('demo_otp',None);data['auth_method']='totp'
        raw = data if isinstance(data, bytes) else json_bytes(data)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Permissions-Policy", "camera=(), geolocation=(), payment=()")
        self.send_header("Content-Security-Policy", "default-src 'self'; connect-src 'self'; media-src 'self' blob:; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; object-src 'none'; base-uri 'self'; frame-ancestors 'none'")
        for cookie in getattr(self,'_identity_response_cookies',[]):self.send_header('Set-Cookie',cookie)
        self._identity_response_cookies=[]
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(raw)

    def _client_key(self) -> str:
        # 不直接信任外部传来的 X-Forwarded-For；生产环境应由受信任网关注入真实客户端标识。
        return self.client_address[0] if self.client_address else "unknown"

    def _body(self) -> Dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > MAX_BODY_BYTES:
            return {}
        raw = self.rfile.read(length) if length else b"{}"
        try:
            value = json.loads(raw.decode("utf-8"))
            return value if isinstance(value, dict) else {}
        except Exception:
            return {}

    def do_GET(self):
        service=getattr(runtime,'identity_service',None)
        try:return service.handle(self,self._do_GET) if service else self._do_GET()
        except (OSError,sqlite3.Error):return self._send(503,{'ok':False,'error_code':'IDENTITY_OR_STORAGE_UNAVAILABLE','message':'身份或存储暂不能核验；未自动重做业务。'})

    def _do_GET(self):
        path = urlparse(self.path).path
        if path=='/api/v1/identity/capabilities':return self._send(200,{'ok':True,'mode':'DEMO','production_certified':False})
        if path=='/support/operator':return self._send(200,(STATIC_DIR/'operator.html').read_bytes(),'text/html; charset=utf-8')
        if path=='/support/operator.js':return self._send(200,OPERATOR_SCRIPT.encode(),'application/javascript; charset=utf-8')
        query = {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}
        family_identity = runtime.family.http_context(
            getattr(self, 'headers', {}).get('Authorization', ''), query,
            required=path in {'/api/state', '/api/audit', '/api/v1/transactions/status'} or path.startswith('/api/v1/audit/') or path.startswith('/api/dag/') or path.startswith('/api/v1/bank/') or path.startswith('/api/v1/support/'))
        if not family_identity['ok']:
            return self._send(403, family_identity)
        if path=='/api/v1/support/capabilities':return self._send(200,{'ok':True,'mode':'SELF_HOSTED_SUPPORT','manual_acceptance_available':False,
            'third_party_bank_customer_service':False,'production_identity':False,'bank_business_executed':False})
        if path=='/api/v1/bank/capabilities':
            return self._send(200,{'ok':True,'mode':runtime.bank_service.service_scope if runtime.bank_service is not None else 'MOCK',
                'live_bank_connected':False,'profile':'project-normalized-v1','callback_public_endpoint':False,
                'reconciliation_available':runtime.bank_service is not None,'production_ready':False})
        if path=='/api/v1/bank/reconciliation':
            if runtime.bank_service is None:return self._send(503,{'ok':False,'status':'ADAPTER_NOT_CONFIGURED','business_executed':False})
            try:
                ctx=runtime.bank_service.context(family_identity['user_id'],device_id=family_identity['device_id'],trace_id=new_id('trace'))
                result=runtime.bank_service.reconcile_statement(ctx)
            except BankServiceError:result={'ok':False,'status':'ACCOUNT_ACCESS_DENIED','business_executed':False}
            return self._send(200 if result['ok'] else 503,result)
        if path == '/api/dag/state':
            response = runtime.dag_control('state', str(query.get('dag_id', '')),
                                           family_identity['user_id'], family_identity['device_id'])
            return self._send(200 if response['ok'] else 403, response)
        if path in {'/api/v1/family/grants', '/api/v1/family/notifications'}:
            if not family_identity.get('family'):
                return self._send(403, runtime.family.deny('家庭入口需要可信登录会话'))
            user = family_identity['user_id']
            items = runtime.bank.guardianships.grants_for(user) if path.endswith('/grants') else runtime.family.guardian_inbox(user)
            return self._send(200, {'ok': True, 'mode': 'offline_identity_fixture', 'items': sanitize_audit_value(items)})
        if path.startswith('/api/v1/audit/') and query.get('trace_id'):
            denied = runtime.family.trace_guard(family_identity, query['trace_id'])
            if denied:
                return self._send(403, denied)
        if path == '/api/v1/transactions/status' and query.get('transaction_id'):
            with runtime.bank.lock:
                row = runtime.bank._row_for_transaction(query['transaction_id'])
            denied = runtime.family.resource_guard(family_identity, row['actor_id']) if row else None
            if denied:
                return self._send(403, denied)
        if path == "/api/v1/security/attack-matrix":
            from security_evaluation import load_attack_matrix
            return self._send(200, {"ok": True, "attacks": load_attack_matrix(), "mode": "isolated_mock_offline"})
        if path == "/readyz":
            readiness = runtime.recovery_readiness()
            return self._send(200 if readiness["ok"] else 503, readiness)
        if path in {"/healthz", "/health/live"}:
            return self._send(200, {"ok": True, "status": "LIVE", "service": "banking-agent", "time": now_iso()})
        if path == "/health/ready":
            readiness = preflight_report()
            status = 200 if readiness.get("status") == "READY_FOR_CONFIGURED_RUNTIME" else 503
            return self._send(status, {"ok": status == 200, "readiness": readiness})
        if path == "/api/health":
            return self._send(200, {"ok": True, "service": "banking-agent", "time": now_iso(), "breaker": runtime.breaker.state(), "capabilities": runtime.capabilities()})
        if path == "/api/v1/voice/capabilities":
            return self._send(200, {"ok": True, **SpeechAdapter().capabilities()})
        if path == '/api/v1/ocr/capabilities':
            return self._send(200, {'ok':True,**runtime.ocr.capabilities()})
        if path == "/api/capabilities":
            caps = runtime.capabilities()
            caps["gateway"] = gateway.capabilities()
            return self._send(200, {"ok": True, "capabilities": caps})
        if path == "/api/gateway/capabilities":
            return self._send(200, {"ok": True, "capabilities": gateway.capabilities()})
        if path == "/api/readiness":
            return self._send(200, {"ok": True, "readiness": runtime.readiness_report()})
        if path == "/api/production/contracts":
            return self._send(200, {"ok": True, "contracts": production_contract_manifest()})
        if path == "/api/preflight":
            return self._send(200, {"ok": True, "preflight": preflight_report()})
        if path in {"/api/v1/notifications", "/api/v1/notifications/events", "/api/v1/notifications/subscriptions"}:
            query = parse_qs(urlparse(self.path).query)
            user_id = (query.get("user_id") or [""])[0].strip()[:128]
            trace_id = (query.get("trace_id") or [""])[0].strip()[:128]
            channel = (query.get("channel") or [""])[0].strip()[:32]
            if not user_id:
                return self._send(400, {"ok": False, "message": "请提供 user_id（演示查询标识，不是身份认证）。"})
            if channel and channel not in NotificationBus.CHANNELS:
                return self._send(400, {"ok": False, "message": "不支持的通知通道。"})
            if path.endswith("/events"):
                items = runtime.notifications.list_events(user_id, trace_id)
            elif path.endswith("/subscriptions"):
                items = runtime.notifications.list_subscriptions(user_id)
            else:
                items = runtime.notifications.list_notifications(user_id, trace_id, channel)
            return self._send(200, {"ok": True, "mode": "local_simulator", "count": len(items), "items": sanitize_audit_value(items)})
        if path == '/api/state' and family_identity.get('user_id') != 'demo-user':
            owner = family_identity['user_id']
            return self._send(200, {'ok': True, 'balance': runtime.bank.balance(owner).data,
                                    'transactions': runtime.bank.recent_transactions(owner).data,
                                    'daily_transfer_cents': runtime.bank.daily_transfer_total_cents(owner),
                                    'daily_transfer_limit_cents': POLICY['daily_transfer_limit_cents'],
                                    'demo_otp': POLICY['demo_otp']})
        if path == "/api/state":
            return self._send(200, {"ok": True, "balance": runtime.bank.balance().data, "daily_transfer_cents": runtime.bank.daily_transfer_cents, "daily_transfer_limit_cents": POLICY["daily_transfer_limit_cents"], "demo_otp": POLICY["demo_otp"], "transactions": runtime.bank.recent_transactions().data, "transfer_recipients": [{"name": name, "account_label": phone[:3] + "****" + phone[-4:]} for name, phone in runtime.bank.beneficiaries.items()], "transfer_recipient_candidates": [{"name": candidate["name"], "account_label": candidate["phone"][:3] + "****" + candidate["phone"][-4:]} for candidates in runtime.bank.beneficiary_candidates.values() for candidate in candidates], "subscriptions": runtime.bank.subscriptions_query().data, "investments": runtime.bank.investments_query().data, "cards": runtime.bank.cards_query().data, "breaker": runtime.breaker.state()})
        if path == "/api/audit":
            return self._send(200, {"ok": True, "items": runtime.family.visible_audit(family_identity)})
        if path == "/api/v1/transactions/status":
            query = parse_qs(urlparse(self.path).query)
            transaction_id = (query.get("transaction_id") or [""])[0].strip()[:128]
            if not transaction_id:
                return self._send(400, {"ok": False, "message": "请提供 transaction_id。"})
            status = runtime.query_transaction(transaction_id)
            return self._send(200 if status["ok"] else 404, status)
        if path == "/api/v1/audit/verify":
            query = parse_qs(urlparse(self.path).query)
            trace_id = (query.get("trace_id") or [""])[0].strip()[:128]
            if not trace_id:
                return self._send(400, {"ok": False, "message": "请提供 trace_id。"})
            return self._send(200, runtime.verify_audit_trace(trace_id))
        if path == "/api/v1/audit/events":
            query = parse_qs(urlparse(self.path).query)
            trace_id = (query.get("trace_id") or [""])[0].strip()[:128]
            if not trace_id:
                return self._send(400, {"ok": False, "message": "请提供 trace_id。"})
            events = runtime.audit.list_for_trace(trace_id)
            return self._send(200, {"ok": True, "trace_id": trace_id, "count": len(events), "events": events})
        if path == "/":
            return self._send(200, (STATIC_DIR / "index.html").read_bytes(), "text/html; charset=utf-8")
        if path == "/api/v1/voice/output-capabilities":
            return self._send(200, TTSOutput().capabilities())
        if path.startswith("/static/"):
            target = (STATIC_DIR / path.removeprefix("/static/")).resolve()
            if STATIC_DIR.resolve() in target.parents and target.exists():
                ctype = {".css": "text/css; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".json": "application/manifest+json; charset=utf-8", ".svg": "image/svg+xml"}.get(target.suffix, "application/octet-stream")
                return self._send(200, target.read_bytes(), ctype)
        self._send(404, {"ok": False, "message": "Not found"})

    def do_HEAD(self):
        # 让健康检查、反向代理和静态资源探测可以使用标准 HEAD。
        return self.do_GET()

    def _transcribe_voice(self):
        # Separate binary endpoint: no change to the existing JSON request limit.
        identity = runtime.family.http_context(self.headers.get('Authorization', ''), {},
                                               required=True, action='voice')
        self.close_connection = True
        trace = 'trace_' + uuid.uuid4().hex
        consent=self.headers.get('X-Audio-Consent')=='yes'
        cloud_consent=self.headers.get('X-Audio-Cloud-Consent')=='yes'
        evidence=None;outbound=False
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:length=0
        def drain_known_body():
            if 0<length<=MAX_AUDIO_BYTES and not self.headers.get('Transfer-Encoding'):
                try:
                    if hasattr(self,'connection'):self.connection.settimeout(1)
                    self.rfile.read(length)
                except (TimeoutError,OSError):pass
        def finish(status, result):
            event=('voice.transcribed' if result.get('text') else 'voice.evidenced') if result.get('ok') else 'voice.rejected'
            runtime.audit.write(trace, event,
                                {'user_id': identity.get('user_id','unknown'), 'device_id': identity.get('device_id','unknown'),
                                 'reason': result.get('reason', 'accepted'), 'operation': 'transcribe',
                                 'source': result.get('source','none'),'consent':consent,'cloud_consent':cloud_consent,
                                 'outbound_attempted':outbound,'audio_evidence':voice_audit_evidence(evidence) if evidence else None,
                                 'provider_receipt':voice_audit_evidence({
                                     'provider_request_id':result['provider_receipt'].get('request_id'),
                                     'http_status':result['provider_receipt'].get('http_status'),
                                     'response_trace_matches':result['provider_receipt'].get('response_trace_matches'),
                                     'deletion_verified':False}) if result.get('provider_receipt') else None,
                                 'retention':'application_request_memory_only'}, actor=identity.get('user_id','unknown'))
            return self._send(status, dict(result, trace_id=trace,business_executed=False,
                                          **({'audio_evidence':evidence} if evidence else {})))
        if not identity['ok']:
            drain_known_body()
            return finish(403,SpeechAdapter.failure('identity_required'))
        if not consent:
            drain_known_body()
            return finish(400, SpeechAdapter.failure('consent_required'))
        if length > MAX_AUDIO_BYTES:
            return finish(413, SpeechAdapter.failure('audio_too_large'))
        if length <= 0 or self.headers.get('Transfer-Encoding'):
            return finish(400, SpeechAdapter.failure('invalid_length'))
        if self.headers.get('Content-Type', '').split(';')[0].lower() != 'audio/wav':
            drain_known_body()
            return finish(415, SpeechAdapter.failure('unsupported_audio'))
        try:
            if hasattr(self, 'connection'):
                self.connection.settimeout(10)
            audio = self.rfile.read(length)
        except (TimeoutError, OSError):
            return finish(408, SpeechAdapter.failure('upload_timeout'))
        if len(audio) != length:
            return finish(400, SpeechAdapter.failure('incomplete_audio'))
        try:
            try:evidence,normalized=prepare_audio(audio)
            except ValueError:return finish(400,SpeechAdapter.failure('invalid_audio'))
            del normalized
            adapter=SpeechAdapter()
            if not adapter.enabled or not cloud_consent:
                return finish(200,{'ok':True,'mode':'local_evidence_only','source':'local_audio_evidence',
                    'message':'已在本机生成音频摘要；未发送云端、未转写、未办理业务。'})
            outbound=True
            result=adapter.transcribe(audio)
            if result.get('ok'):
                try:token=runtime.voice_evidence.issue(evidence,identity['user_id'],identity['device_id'],trace)
                except ValueError:return finish(429,SpeechAdapter.failure('evidence_limit'))
                result=dict(result,voice_source_token=token,requires_text_review=True)
        finally:
            del audio
        return finish(200 if result['ok'] else (400 if result['reason'] == 'invalid_audio' else 503), result)

    def _extract_ocr(self):
        identity=runtime.family.http_context(self.headers.get('Authorization',''),{},required=True,action='ocr')
        self.close_connection=True
        try:length=int(self.headers.get('Content-Length','0'))
        except ValueError:length=0
        def drain_known_body():
            if not self.headers.get('Transfer-Encoding') and 0<length<=MAX_IMAGE_BYTES:
                try:
                    self.connection.settimeout(1);self.rfile.read(length)
                except (TimeoutError,OSError):pass
        if not identity['ok'] or identity.get('family'):
            # Closing with unread upload bytes can discard the 403 via a TCP
            # reset on Windows. Drain only a bounded, known-size body, never infer.
            drain_known_body()
            return self._send(403,{'ok':False,'reason':'identity_or_scope_denied','message':'图片草稿仅向当前本人会话开放。'})
        trace='trace_'+uuid.uuid4().hex
        if self.headers.get('X-OCR-Consent')!='yes':
            drain_known_body()
            result=OCRService.failure('consent_required')
        elif not 0<length<=MAX_IMAGE_BYTES or self.headers.get('Transfer-Encoding'):
            result=OCRService.failure('invalid_size')
        else:
            try:
                self.connection.settimeout(10);raw=self.rfile.read(length)
                result=(runtime.ocr.extract(raw,self.headers.get('Content-Type','').split(';')[0].lower(),
                    identity['user_id'],identity['device_id'],consent=True,trace_id=trace)
                    if len(raw)==length else OCRService.failure('incomplete_image'))
            except (TimeoutError,OSError):result=OCRService.failure('upload_timeout')
        runtime.audit.write(trace,'ocr.extracted' if result['ok'] else 'ocr.rejected',
            {'reason':result.get('reason','accepted'),'sha256':result.get('sha256',''),'device_id':identity['device_id'],
             'fields':list(result.get('fields',{})),'flags':result.get('flags',[]),'source':'local_ocr_untrusted_draft'},actor=identity['user_id'])
        return self._send(200 if result['ok'] else (429 if result.get('reason')=='busy' else 400),dict(result,trace_id=trace))

    def do_POST(self):
        service=getattr(runtime,'identity_service',None)
        if service and urlparse(self.path).path.startswith('/api/v1/identity/') and not http_rate_limiter.allow(self._client_key()):
            return self._send(429,{'ok':False,'message':'认证请求过于频繁，请稍后再试。'})
        try:return service.handle(self,self._do_POST) if service else self._do_POST()
        except (OSError,sqlite3.Error):return self._send(503,{'ok':False,'error_code':'IDENTITY_OR_STORAGE_UNAVAILABLE','message':'身份或存储暂不能核验；请查询原业务，不要重复提交。'})

    def _do_POST(self):
        if not http_rate_limiter.allow(self._client_key()):
            return self._send(429, {"ok": False, "message": "请求过于频繁，请稍后再试。"})
        path = urlparse(self.path).path
        if path == '/api/v1/voice/transcribe':
            return self._transcribe_voice()
        if path == '/api/v1/ocr/extract':
            return self._extract_ocr()
        body = self._body()
        family_identity = runtime.family.http_context(
            getattr(self, 'headers', {}).get('Authorization', ''), body,
            required=path in {'/api/chat', '/api/confirm', '/api/verify', '/api/cancel',
                             '/api/linkage/cancel', '/api/v1/tasks/status', '/api/v1/audit/unlock', '/api/reset-demo',
                             '/api/v1/customer-service'} or path.startswith(('/api/dag/', '/api/v1/handoff/', '/api/v1/ocr/','/api/v1/voice/','/api/v1/support/')),
            action=path.rsplit('/', 1)[-1])
        if not family_identity['ok']:
            return self._send(403, family_identity)
        if path == '/api/v1/voice/synthesize':
            result = TTSOutput().synthesize(body.get('text'), consent=body.get('consent'),
                voice=body.get('voice', 'zh-CN-XiaoxiaoNeural'), rate=body.get('rate', -8))
            if result['ok']:
                return self._send(200, result['audio'], 'audio/mpeg')
            return self._send(400 if result['reason'] in ('invalid_request', 'consent_required') else 503, result)
        if path.startswith('/api/v1/support/'):
            action=path.rsplit('/',1)[-1]
            fields={'case_id','message_id','text'} if action=='send' else {'case_id','after_seq'} if action=='poll' else {'case_id','upto_seq'} if action=='ack' else set()
            try:
                if not fields or set(body)-fields-{'user_id','device_id','elder_mode'}:raise ValueError('invalid support fields')
                if action=='send':result={'ok':True,'message':runtime.live_support.send(body.get('case_id'),family_identity,body.get('message_id'),body.get('text'))}
                elif action=='poll':result=runtime.live_support.poll(body.get('case_id'),family_identity,body.get('after_seq',0))
                else:result=runtime.live_support.ack(body.get('case_id'),family_identity,body.get('upto_seq'))
                return self._send(200,result)
            except PermissionError:return self._send(403,{'ok':False,'message':'仅工单本人或已领取的绑定坐席可查看该对话。'})
            except ValueError:return self._send(400,{'ok':False,'message':'请核对工单、消息编号和正文；不要发送密码、验证码、密钥或账号。已结单请发起新求助。'})
            except Exception:return self._send(503,{'ok':False,'message':'对话结果暂不能确认；请保留原消息编号查询或重试，不要重做银行业务。'})
        if family_identity.get('managed'):
            body.update(user_id=family_identity['user_id'], device_id=family_identity['device_id'])
        def send_agent(status, response):
            if response.get("error_code") == "RECOVERY_UNAVAILABLE":
                status = 503
            return self._send(status, present_plain_response(response, body.get("elder_mode") is True))
        if path == '/api/v1/voice/deletion-request':
            result=runtime.voice_deletion_request(body.get('trace_id'),family_identity['user_id'],family_identity['device_id'])
            return self._send(200 if result.get('ok') else 403,result)
        if path == '/api/v1/ocr/review':
            if family_identity.get('family'):return self._send(403,OCRService.failure('family_scope_denied'))
            try:
                if set(body)-{'draft_id','recipient','amount','reviewed','acknowledged_flags','elder_mode','user_id','device_id'}:raise ValueError('extra fields')
                result=runtime.ocr.review(body.get('draft_id'),family_identity['user_id'],family_identity['device_id'],
                    recipient=body.get('recipient'),amount=body.get('amount'),reviewed=body.get('reviewed'),acknowledged_flags=body.get('acknowledged_flags'))
                runtime.audit.write(result['source_trace_id'],'ocr.reviewed',
                    {'sha256':result['sha256'],'device_id':family_identity['device_id'],'source':'human_reviewed_untrusted_draft'},actor=family_identity['user_id'])
                return self._send(200,result)
            except PermissionError:return self._send(403,OCRService.failure('draft_binding_or_expiry'))
            except (ValueError,TypeError):return self._send(400,OCRService.failure('review_required'))
        if path.startswith('/api/v1/handoff/'):
            action = path.rsplit('/', 1)[-1]
            if action != 'status' and family_identity.get('role') != 'admin':
                return self._send(403, runtime.family.deny('本地坐席操作需要受信运维会话', actor=family_identity.get('user_id', '')))
            allowed = {'user_id', 'device_id', 'offset', 'elder_mode'} if action == 'inbox' else {'user_id', 'device_id', 'case_id', 'resolution', 'elder_mode'}
            try:
                if action not in {'status', 'inbox', 'claim', 'resolve'} or set(body) - allowed:
                    raise ValueError('不支持的本地工单请求字段。')
                if action == 'inbox':
                    offset = body.get('offset', 0)
                    items = runtime.handoffs.inbox(offset)
                    return self._send(200, {'ok': True, 'mode': 'LOCAL_DEMO_ONLY', 'items': items,
                                            'next_offset': offset + len(items) if len(items) == 50 else None})
                case_id = body.get('case_id')
                if not isinstance(case_id, str) or not re.fullmatch(r'case_[a-p]{32}', case_id):
                    raise ValueError('请提供有效的工单标识。')
                if action == 'status':
                    case = runtime.handoffs.status(case_id, family_identity['user_id'], family_identity['device_id'])
                else:
                    resolution = body.get('resolution', '')
                    if not isinstance(resolution, str):
                        raise ValueError('工单结论必须是固定类型。')
                    case = runtime.handoffs.transition(case_id, action, family_identity['user_id'], family_identity['device_id'], resolution)
                return self._send(200, {'ok': True, 'case': case})
            except PermissionError:
                return self._send(403, {'ok': False, 'message': '当前身份无法查看或操作该工单。'})
            except ValueError:
                return self._send(400, {'ok': False, 'message': '工单请求无效；请核对字段与允许的处理结论。'})
            except HandoffConflict:
                return self._send(409, {'ok': False, 'message': '工单状态或坐席绑定不允许本次操作。'})
            except Exception:
                return self._send(503, {'ok': False, 'message': '暂时无法确认本地工单结果；请查询原工单，不要重复办理银行业务。'})
        if path in {'/api/dag/propose','/api/dag/adopt'}:
            allowed={'plan_id','proposal','action_token','mode','model_consent','user_id','device_id','elder_mode'}
            if set(body)-allowed:return self._send(400,{'ok':False,'error_code':'INVALID_DAG_FIELDS'})
            result=runtime.dynamic_dag(path.rsplit('/',1)[-1],str(body.get('plan_id','')),family_identity['user_id'],family_identity['device_id'],
                proposal=body.get('proposal'),action_token=body.get('action_token',''),mode=body.get('mode','offline'),model_consent=body.get('model_consent'))
            return self._send(200 if result.get('ok') else 400,result)
        if path in {'/api/dag/pause', '/api/dag/resume', '/api/dag/cancel', '/api/dag/approve'}:
            response = runtime.dag_control(path.rsplit('/', 1)[-1], str(body.get('dag_id', '')),
                family_identity['user_id'], family_identity['device_id'],
                str(body.get('action_token', '')), str(body.get('credential_id', '')),
                str(body.get('decision', 'approve')))
            return send_agent(200 if response.get('ok') else 403, response)
        if path == '/api/v1/family/chat':
            if not family_identity.get('family'):
                return self._send(403, runtime.family.deny('家庭入口需要可信登录会话'))
            response = runtime.family.chat(str(body.get('message', ''))[:2000], body['user_id'], body['device_id'],
                                           str(body.get('subject_id', '')), str(body.get('mode', 'delegate')))
            return send_agent(200 if response.get('ok') else 403, response)
        if path == "/api/v1/tasks/status":
            try:
                response = runtime.task_status(str(body.get("task_id", "")), str(body.get("action_token", "")),
                                               str(body.get("user_id", "")), str(body.get("device_id", "")))
            except sqlite3.Error:
                runtime.recovery_failed = True
                response = runtime.recovery_unavailable()
            return send_agent(200 if response["ok"] else 403, response)
        if path == "/api/v1/customer-service":
            try:
                response = runtime.request_customer_service(body.get("request_id", ""), body.get("user_id", "demo-user"), body.get("device_id", "demo-device"))
            except ValueError:
                return send_agent(400, {"ok": False, "message": "请提供有效的客服请求标识。"})
            except Exception:
                return send_agent(503, {"ok": False, "message": "暂时无法确认客服请求是否已记录，请通过银行官方渠道联系。重试时请使用同一请求，不要重复提交新请求。"})
            return send_agent(200, response)
        if path == "/api/v1/security/attack-matrix/run":
            # 固定服务端语料；请求体不能指定工具、路径、故障或目标账户。
            from security_evaluation import run_attack_matrix
            if not attack_matrix_run_lock.acquire(blocking=False):
                return self._send(409, {"ok": False, "message": "已有安全演练正在运行，请稍后再试。"})
            try:
                return self._send(200, run_attack_matrix(runtime_factory=AgentRuntime))
            finally:
                attack_matrix_run_lock.release()
        if path == "/api/chat":
            text = str(body.get("message", ""))[:2000]
            if not text:
                return self._send(400, {"ok": False, "message": "请告诉我你想办理什么业务。"})
            channel = str(body.get("channel", "web"))[:32] or "web"
            if channel not in gateway.SUPPORTED_CHANNELS:
                return self._send(400, {"ok": False, "message": "暂不支持该消息入口。"})
            user_id = str(body.get("user_id", "demo-user"))[:128] or "demo-user"
            conversation_id = str(body.get("conversation_id", "web-demo"))[:128] or "web-demo"
            device_id = str(body.get("device_id", "demo-device"))[:128] or "demo-device"
            if family_identity.get('managed'):
                evidence=None
                voice_evidence=None
                if body.get('ocr_review_token') and body.get('voice_source_token'):
                    return self._send(403,SpeechAdapter.failure('conflicting_draft_sources'))
                if body.get('voice_source_token'):
                    try:voice_evidence=runtime.voice_evidence.consume(body['voice_source_token'],user_id,device_id,
                                                                     reviewed=body.get('voice_reviewed'))
                    except PermissionError:return self._send(403,SpeechAdapter.failure('invalid_voice_evidence'))
                if body.get('ocr_review_token'):
                    try:evidence=runtime.ocr.consume_review(body['ocr_review_token'],user_id,device_id)
                    except (PermissionError,TypeError):return self._send(403,OCRService.failure('invalid_review_evidence'))
                response=runtime.chat(text, actor_id=user_id, device_id=device_id, session_id=conversation_id, channel=channel)
                if evidence and response.get('trace_id'):
                    runtime.audit.write(response['trace_id'],'ocr.linked',dict(evidence,source='human_reviewed_untrusted_draft'),actor=user_id)
                if voice_evidence and response.get('trace_id'):
                    runtime.audit.write(response['trace_id'],'voice.linked',dict(voice_audit_evidence(voice_evidence),
                        source='human_reviewed_untrusted_voice_draft',device_id=device_id),actor=user_id)
                return send_agent(200,response)
            message = UnifiedMessage(channel=channel, user_id=user_id, conversation_id=conversation_id, text=text, metadata={"device_id": device_id})
            return send_agent(200, gateway.handle(message))
        if path == "/api/confirm":
            return send_agent(200, runtime.confirm(str(body.get("plan_id", "")), str(body.get("action_token", "")), str(body.get("credential_id", "")), str(body.get("device_id", "")), str(body.get("user_id", ""))))
        if path == "/api/verify":
            return send_agent(200, runtime.verify(str(body.get("plan_id", "")), str(body.get("action_token", "")), str(body.get("code", "")), str(body.get("credential_id", "")), str(body.get("device_id", "")), str(body.get("user_id", ""))))
        if path == "/api/cancel":
            return send_agent(200, runtime.cancel(str(body.get("plan_id", "")), str(body.get("action_token", "")), str(body.get("user_id", "")), str(body.get("device_id", ""))))
        if path == "/api/linkage/cancel":
            return send_agent(200, runtime.cancel_linkage_task(str(body.get("task_id", "")), str(body.get("user_id", "demo-user")), str(body.get("device_id", ""))))
        if path in {'/api/v1/audit/unlock', '/api/reset-demo'} and family_identity.get('role') != 'admin':
            return self._send(403, runtime.family.deny('此操作需要受信人工运维会话', actor=family_identity.get('user_id', '')))
        if path == "/api/v1/audit/unlock":
            actor_id = str(body.get("target_user_id", ""))[:128].strip()
            if not actor_id:
                return self._send(400, {"ok": False, "message": "请提供 target_user_id。"})
            return self._send(200, {"ok": True, "user_id": actor_id, "unlocked": runtime.unlock_user(actor_id)})
        if path == "/api/reset-demo":
            runtime.audit.write(new_id("trace"), "security.reset", {"user_id": family_identity["user_id"], "device_id": family_identity["device_id"]}, actor=family_identity["user_id"])
            runtime.breaker.reset()
            return self._send(200, {"ok": True, "message": "安全状态已重置。"})
        self._send(404, {"ok": False, "message": "Not found"})

    def log_message(self, fmt, *args):
        print(f"[{now_iso()}] {self.address_string()} {fmt % args}")


def main() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    runtime.recovery_scheduler.start()
    print(f"Banking AI Agent demo running at http://{HOST}:{server.server_address[1]}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        runtime.recovery_scheduler.stop()
        server.server_close()


if __name__ == "__main__":
    main()
