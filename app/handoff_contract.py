"""Structured, least-privilege human handoff cards.

A handoff card carries enough verified context for a human operator to continue
responsibly, but never carries the original conversation or authentication
secrets.  The demo returns a *process path*, not a bank-issued credential.
Production replacement point: map this contract to the bank's CRM/case system
and its field-level data-loss-prevention policy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Mapping, Optional


_DROP_KEYS = frozenset({
    "raw_conversation", "conversation", "conversation_text", "chat_history",
    "messages", "transcript", "prompt", "system_prompt", "otp", "code",
    "verification_code", "token", "consent_token", "credential", "secret",
    "password", "pin", "cvv", "authorization", "authorization_header",
})
_CARD_KEYS = frozenset({"card", "card_number", "bank_card", "pan", "account_number"})
_AMOUNT_KEYS = frozenset({"amount", "amount_cents", "balance_cents", "fee_cents", "price_cents"})


def _key_name(key: Any) -> str:
    return str(key).strip().lower().replace("-", "_")


def _is_drop_key(key: Any) -> bool:
    name = _key_name(key)
    return name in _DROP_KEYS or any(part in name for part in ("raw_conversation", "transcript", "chat_history"))


def _mask_card(value: Any) -> str:
    digits = "".join(ch for ch in str(value) if ch.isdigit())
    return "****" + (digits[-4:] if len(digits) >= 4 else digits)


def redact(value: Any, key: Any = "") -> Any:
    """Return JSON-safe handoff data with secrets and raw dialogue removed."""
    if _is_drop_key(key):
        return None
    name = _key_name(key)
    if isinstance(value, Mapping):
        result: Dict[str, Any] = {}
        for child_key, child_value in value.items():
            if _is_drop_key(child_key):
                continue
            cleaned = redact(child_value, child_key)
            if cleaned is not None:
                result[str(child_key)] = cleaned
        return result
    if isinstance(value, (list, tuple)):
        return [cleaned for item in value if (cleaned := redact(item, key)) is not None]
    if name in _CARD_KEYS or "card_number" in name or name.endswith("_pan"):
        return _mask_card(value)
    if isinstance(value, bool) or value is None or type(value) in (int, float, str):
        # Monetary values are preserved as integer cents; no conversion is done.
        return value
    return str(value)


def _plain(value: Any) -> Dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, Mapping):
            return dict(converted)
    try:
        converted = asdict(value)
    except (TypeError, ValueError):
        converted = {}
    return converted if isinstance(converted, Mapping) else {}


def _step_dict(step: Any) -> Dict[str, Any]:
    raw = _plain(step)
    return {
        "step_id": raw.get("id", raw.get("node_id", "")),
        "tool": raw.get("tool", raw.get("node_type", "")),
        "status": raw.get("status", "pending"),
    }


def _steps(source: Mapping[str, Any]) -> List[Dict[str, Any]]:
    raw_steps = source.get("steps") or source.get("nodes") or []
    return [_step_dict(step) for step in raw_steps if isinstance(_plain(step), Mapping)]


@dataclass
class HandoffCard:
    user_goal: str
    verified_information: Dict[str, Any] = field(default_factory=dict)
    authorization_scope: Dict[str, Any] = field(default_factory=dict)
    executed_steps: List[Dict[str, Any]] = field(default_factory=list)
    business_identifiers: Dict[str, Any] = field(default_factory=dict)
    pending_items: List[str] = field(default_factory=list)
    redacted_context: Dict[str, Any] = field(default_factory=dict)
    created_at: str = ""

    def __post_init__(self) -> None:
        self.user_goal = str(self.user_goal or "未明确的用户目标")
        self.verified_information = redact(self.verified_information) or {}
        self.authorization_scope = redact(self.authorization_scope) or {}
        self.executed_steps = redact(self.executed_steps) or []
        self.business_identifiers = redact(self.business_identifiers) or {}
        self.pending_items = [str(item) for item in (self.pending_items or [])]
        self.redacted_context = redact(self.redacted_context) or {}
        self.created_at = self.created_at or datetime.now(timezone.utc).isoformat()

    @classmethod
    def from_plan(cls, plan: Any, trigger: str, pending_items: Optional[Iterable[str]] = None,
                  context: Optional[Mapping[str, Any]] = None) -> "HandoffCard":
        source = _plain(plan)
        entities = source.get("entities") if isinstance(source.get("entities"), Mapping) else {}
        verified_keys = ("beneficiary", "beneficiary_account", "amount_cents", "subscription_name",
                         "subscription_id", "member", "allowance_cents", "card_number", "account_number")
        verified = {key: entities[key] for key in verified_keys if key in entities and entities[key] is not None}
        verified["intent"] = source.get("intent", "")
        steps = _steps(source)
        pending = list(pending_items or [])
        pending.extend(_pending_step_text(steps))
        goal_labels = {"transfer": "本次转账", "birthday_linkage": "生日联动",
                       "cancel_subscription": "订阅管理", "customer_service": "人工客服请求"}
        return cls(
            user_goal=source.get("summary") or goal_labels.get(source.get("intent"), source.get("intent")) or "用户请求人工协助",
            verified_information=verified,
            authorization_scope={"actor_id": source.get("actor_id", ""), "device_id": source.get("device_id", ""),
                                 "scope": "仅限本次计划；人工继续办理前需重新核验权限"},
            executed_steps=[step for step in steps if step.get("status") in {"completed", "succeeded", "compensated"}],
            business_identifiers={key: source[key] for key in ("plan_id", "trace_id") if source.get(key)},
            pending_items=pending or ["由人工确认下一步办理路径"],
            redacted_context={"trigger": trigger, "risk_level": source.get("risk_level", ""),
                              "amount_cents": entities.get("amount_cents"), **dict(context or {})},
        )

    @classmethod
    def from_dag(cls, dag: Any, trigger: str, pending_items: Optional[Iterable[str]] = None,
                 context: Optional[Mapping[str, Any]] = None) -> "HandoffCard":
        source = _plain(dag)
        nodes = source.get("nodes") or []
        metadata = source.get("metadata") or {}
        entities = dict(metadata.get("entities") or {})
        for node in nodes:
            params = _plain(node).get("params") or {}
            for key, value in params.items():
                if value is not None and key not in entities:
                    entities[key] = value
        plan_like = {
            "plan_id": source.get("plan_id", ""), "trace_id": source.get("trace_id", ""),
            "intent": source.get("intent", ""), "actor_id": source.get("actor_id", ""),
            "device_id": metadata.get("device_id", ""),
            "summary": metadata.get("summary") or source.get("intent", ""),
            "steps": nodes, "entities": entities,
            "risk_level": metadata.get("risk_level", ""),
        }
        card = cls.from_plan(plan_like, trigger, pending_items=pending_items, context=context)
        identifiers = dict(card.business_identifiers)
        identifiers["dag_id"] = source.get("dag_id", "")
        transaction_ids = _find_transaction_ids(nodes)
        if transaction_ids:
            identifiers["transaction_ids"] = transaction_ids
        card.business_identifiers = redact(identifiers) or {}
        card.redacted_context.update({"dag_status": source.get("status", "")})
        return card

    @classmethod
    def for_request(cls, actor_id: str, device_id: str, trace_id: str, request_id: str,
                    trigger: str = "user_requested") -> "HandoffCard":
        return cls(
            user_goal="用户主动请求人工帮助",
            verified_information={"actor_id": actor_id, "device_id": device_id},
            authorization_scope={"scope": "仅提交客服请求，不代表已授权任何资金或账户操作"},
            executed_steps=[],
            business_identifiers={"trace_id": trace_id, "request_id": request_id},
            pending_items=["由官方人工客服核实身份、目标和可办理事项"],
            redacted_context={"trigger": trigger, "path": "演示系统提供办理路径，不生成正式银行凭据"},
        )

    def to_dict(self) -> Dict[str, Any]:
        return redact({
            "user_goal": self.user_goal,
            "verified_information": self.verified_information,
            "authorization_scope": self.authorization_scope,
            "executed_steps": self.executed_steps,
            "business_identifiers": self.business_identifiers,
            "pending_items": self.pending_items,
            "redacted_context": self.redacted_context,
            "created_at": self.created_at,
        })


def _pending_step_text(steps: List[Dict[str, Any]]) -> List[str]:
    return [f"步骤 {step.get('step_id') or '未命名'} 待人工核实" for step in steps
            if step.get("status") not in {"completed", "succeeded", "failed", "compensated"}]


def _find_transaction_ids(nodes: Any) -> List[str]:
    found: List[str] = []
    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if key == "transaction_id" and isinstance(item, str) and item not in found:
                    found.append(item)
                else:
                    visit(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                visit(item)
    visit(nodes)
    return found
