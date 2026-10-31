"""只读支付预检。

预检是计划卡上的事实报告，不是权限裁判，也不是成功保证。
生产替换点：把 MockBankCore 的只读查询适配为核心银行的余额、额度、收款人
和卡状态查询；执行权限仍由 PolicyEngine 与副作用前重校验决定。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


STATUSES = frozenset({"satisfied", "missing", "unknown"})


def _display_cents(value: int) -> str:
    """仅用于展示整数分，不参与业务计算。"""
    whole, fraction = divmod(abs(value), 100)
    sign = "-" if value < 0 else ""
    return f"¥{sign}{whole:,}.{fraction:02d}"


@dataclass(frozen=True)
class PreflightDimension:
    key: str
    label: str
    status: str
    message: str
    evidence: Dict[str, Any]

    def __post_init__(self) -> None:
        if self.status not in STATUSES:
            raise ValueError(f"未知预检状态: {self.status}")


@dataclass(frozen=True)
class PreflightReport:
    """支付计划的只读预检结果。

    ``PreflightReport`` 支持 ``report["checks"]``，便于 HTTP/测试代码使用，
    但内部仍保留结构化类型；它不包含或生成任何执行授权。
    """

    checks: Dict[str, Dict[str, Any]]
    generated_at: str
    actor_id: str
    amount_cents: Optional[int]
    beneficiary: Optional[str]
    read_only: bool = True
    disclaimer: str = "预检只报告当前可见事实，不代表银行已受理或执行成功。"
    budget_options: list[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        ordered = list(self.checks.values())
        missing = [item["label"] for item in ordered if item["status"] == "missing"]
        unknown = [item["label"] for item in ordered if item["status"] == "unknown"]
        messages = [item["message"] for item in ordered]
        return {
            "checks": self.checks,
            "generated_at": self.generated_at,
            "actor_id": self.actor_id,
            "amount_cents": self.amount_cents,
            "beneficiary": self.beneficiary,
            "missing": missing,
            "unknown": unknown,
            "messages": messages,
            "read_only": self.read_only,
            "disclaimer": self.disclaimer,
            "budget_options": list(self.budget_options),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)


def _dimension(key: str, label: str, status: str, message: str, **evidence: Any) -> Dict[str, Any]:
    return asdict(PreflightDimension(key, label, status, message, evidence))


def _result_data(result: Any) -> Optional[Dict[str, Any]]:
    if result is None or not getattr(result, "ok", False):
        return None
    data = getattr(result, "data", None)
    return data if isinstance(data, dict) else None


def build_payment_preflight(
    bank: Any,
    policy: Dict[str, Any],
    *,
    amount_cents: Optional[int],
    beneficiary: Optional[str],
    actor_id: str = "demo-user",
    account_requires_verification: bool = False,
    requires_confirmation: bool = False,
    requires_strong_auth: bool = False,
    beneficiary_verified: Optional[bool] = None,
) -> PreflightReport:
    """生成支付预检报告。

    只调用核心的只读接口（余额、流水累计、收款人目录、卡状态），不会调用
    ``check_transfer_constraints``，避免预检改变其滚动日累计等内部状态。
    ``policy`` 是已校验的不可变策略快照；阈值不在本模块硬编码。
    """
    checks: Dict[str, Dict[str, Any]] = {}
    valid_amount = type(amount_cents) is int and amount_cents > 0

    # 1. 可用资金：只读余额快照。
    balance_data = None
    try:
        balance_data = _result_data(bank.balance(actor_id))
    except (AttributeError, KeyError, TypeError, ValueError):
        balance_data = None
    if not valid_amount or balance_data is None or type(balance_data.get("available_cents")) is not int:
        checks["available_funds"] = _dimension(
            "available_funds", "可用资金", "unknown",
            "可用资金暂时未知，请以银行最新查询结果为准。",
            amount_cents=amount_cents,
        )
    else:
        available = balance_data["available_cents"]
        status = "satisfied" if available >= amount_cents else "missing"
        message = (
            f"可用资金足够：当前可用 {_display_cents(available)}，本次需要 {_display_cents(amount_cents)}。"
            if status == "satisfied"
            else f"可用资金不足：当前可用 {_display_cents(available)}，本次需要 {_display_cents(amount_cents)}。"
        )
        checks["available_funds"] = _dimension(
            "available_funds", "可用资金", status, message,
            available_cents=available, amount_cents=amount_cents,
        )

    # 2. 日累计限额：直接读取流水事实源，边界等于限额时通过。
    daily_total = None
    try:
        daily_total = bank.daily_transfer_total_cents(actor_id)
    except (AttributeError, KeyError, TypeError, ValueError):
        daily_total = None
    daily_limit = policy.get("daily_transfer_limit_cents")
    if not valid_amount or type(daily_total) is not int or type(daily_limit) is not int:
        checks["daily_limit"] = _dimension(
            "daily_limit", "日累计限额", "unknown",
            "当日累计额度暂时未知，请以银行最新查询结果为准。",
            current_cents=daily_total, amount_cents=amount_cents, limit_cents=daily_limit,
        )
    else:
        projected = daily_total + amount_cents
        status = "satisfied" if projected <= daily_limit else "missing"
        message = (
            f"日累计额度满足：当前累计 {_display_cents(daily_total)}，本次后 {_display_cents(projected)}，限额 {_display_cents(daily_limit)}。"
            if status == "satisfied"
            else f"日累计额度不足：本次后将达到 {_display_cents(projected)}，超过限额 {_display_cents(daily_limit)}。"
        )
        checks["daily_limit"] = _dimension(
            "daily_limit", "日累计限额", status, message,
            current_cents=daily_total, amount_cents=amount_cents,
            projected_cents=projected, limit_cents=daily_limit,
        )

    # 3. 收款人核验：亲属名称不自动豁免核验；目录未知也不猜测。
    if beneficiary_verified is True and not account_requires_verification:
        recipient_status = "satisfied"
        recipient_message = "收款人已完成核验。"
        recipient_evidence = {"name": beneficiary, "verified": True}
    elif not beneficiary:
        recipient_status = "unknown"
        recipient_message = "收款人尚未明确，无法判断是否已核验。"
        recipient_evidence = {"name": beneficiary, "verified": None}
    else:
        known = False
        try:
            known = beneficiary in getattr(bank, "beneficiaries", {})
        except TypeError:
            known = False
        if account_requires_verification:
            recipient_status = "missing"
            recipient_message = "收款账户待核验；亲属称呼不等于已核验账户。"
            recipient_evidence = {"name": beneficiary, "verified": False, "account_requires_verification": True}
        elif known:
            recipient_status = "satisfied"
            recipient_message = f"收款人“{beneficiary}”在演示收款人目录中，可继续由执行前校验确认。"
            recipient_evidence = {"name": beneficiary, "verified": True, "directory_match": True}
        else:
            recipient_status = "missing"
            recipient_message = f"收款人“{beneficiary}”是新收款人，执行前还需要完成收款人核验。"
            recipient_evidence = {"name": beneficiary, "verified": False, "new_recipient": True}
    checks["beneficiary"] = _dimension("beneficiary", "收款人核验", recipient_status, recipient_message, **recipient_evidence)

    # 4. 卡状态：没有可信卡片状态时明确未知，不把空列表当正常。
    card_data = None
    try:
        card_data = _result_data(bank.cards_query())
    except (AttributeError, KeyError, TypeError, ValueError):
        card_data = None
    cards = card_data.get("items") if card_data is not None else None
    if (card_data is not None and card_data.get('service_scope')=='FINERACT_LOCAL_SIMULATOR'
            and card_data.get('payment_rail')=='savings_account_transfer' and card_data.get('account_active') is True
            and card_data.get('account_owner_id')==actor_id and card_data.get('card_management_supported') is False):
        card_status='satisfied'
        card_message='本次使用已核验的储蓄账户转账路径，不涉及银行卡；卡管服务未接入。'
        card_evidence={'applicable':False,'payment_rail':'savings_account_transfer','account_ref':card_data.get('account_ref'),
                       'source':'verified Fineract account; not card status','card_management_supported':False}
    elif not isinstance(cards, list) or not cards:
        card_status = "unknown"
        card_message = "付款卡状态未知，请先查询可用卡片。"
        card_evidence = {"cards": [] if cards == [] else None}
    else:
        statuses = [item.get("status") if isinstance(item, dict) else None for item in cards]
        if any(status in {"受限", "冻结", "挂失", "restricted", "frozen", "lost"} for status in statuses):
            card_status = "missing"
            card_message = "存在受限付款卡，当前不能据此确认可办理。"
        elif all(status in {"正常", "normal", "active"} for status in statuses):
            card_status = "satisfied"
            card_message = "付款卡状态正常。"
        else:
            card_status = "unknown"
            card_message = "付款卡状态未知，请先核实卡片状态。"
        card_evidence = {"statuses": statuses}
    checks["card_status"] = _dimension("card_status", "卡状态", card_status, card_message, **card_evidence)

    # 5. 认证条件：只报告 PolicyEngine 已作出的要求，不把预检当成认证。
    if requires_strong_auth:
        auth_status = "missing"
        auth_message = "本次操作需要强认证，当前预检不会代替验证码或其他强验证。"
        auth_evidence = {"required": "strong_auth", "confirmed": False}
    elif requires_confirmation:
        auth_status = "missing"
        auth_message = "本次操作需要你在计划卡上明确确认，当前预检不会代替确认。"
        auth_evidence = {"required": "confirmation", "confirmed": False}
    else:
        auth_status = "satisfied"
        auth_message = "当前策略未要求额外认证步骤。"
        auth_evidence = {"required": "none", "confirmed": True}
    checks["authentication"] = _dimension("authentication", "认证条件", auth_status, auth_message, **auth_evidence)

    # 6. 预算约束：只读计算“余额 - 已确认支出 - 安全缓冲”。预计收入不入账。
    budget_options = []
    commitment_data = None
    try:
        commitment_data = _result_data(bank.confirmed_budget_commitments(actor_id))
    except (AttributeError, KeyError, TypeError, ValueError):
        commitment_data = None
    buffer_cents = policy.get("preflight_security_buffer_cents")
    commitment_items = commitment_data.get("items") if commitment_data is not None else None
    if (not valid_amount or balance_data is None or type(balance_data.get("available_cents")) is not int
            or not isinstance(commitment_items, list) or type(buffer_cents) is not int):
        checks["budget"] = _dimension(
            "budget", "预算约束", "unknown",
            "预算约束暂时未知，请先核实可用余额和已确认支出；系统不会用预计收入补足。",
            available_cents=balance_data.get("available_cents") if balance_data else None,
            confirmed_expenses_cents=None,
            security_buffer_cents=buffer_cents,
            disposable_cents=None,
            projected_disposable_cents=None,
            shortfall_cents=None,
            projected_income_cents=None,
            commitments=None,
        )
    else:
        confirmed_expenses_cents = sum(item["amount_cents"] for item in commitment_items)
        available_cents = balance_data["available_cents"]
        disposable_cents = available_cents - confirmed_expenses_cents - buffer_cents
        projected_disposable_cents = disposable_cents - amount_cents
        shortfall_cents = max(0, -projected_disposable_cents)
        budget_status = "satisfied" if projected_disposable_cents >= 0 else "missing"
        if budget_status == "satisfied":
            budget_message = (
                f"扣除已确认支出 {_display_cents(confirmed_expenses_cents)} 和安全缓冲 {_display_cents(buffer_cents)} 后，"
                f"计划内可支配 {_display_cents(disposable_cents)}；本次转账后仍余 {_display_cents(projected_disposable_cents)}。"
            )
        else:
            budget_message = (
                f"本次转账会造成预算缺口 {_display_cents(shortfall_cents)}；"
                f"扣除房租、还款、待付款订阅和安全缓冲后，不会自动借款或擅自降额执行。"
            )
            budget_options = ["adjust_amount", "adjust_budget", "defer", "human_handoff"]
        checks["budget"] = _dimension(
            "budget", "预算约束", budget_status, budget_message,
            available_cents=available_cents,
            confirmed_expenses_cents=confirmed_expenses_cents,
            security_buffer_cents=buffer_cents,
            disposable_cents=disposable_cents,
            projected_disposable_cents=projected_disposable_cents,
            shortfall_cents=shortfall_cents,
            projected_income_cents=commitment_data.get("projected_income_cents", 0),
            commitments=[dict(item) for item in commitment_items],
        )

    return PreflightReport(
        checks=checks,
        generated_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        actor_id=actor_id,
        amount_cents=amount_cents,
        beneficiary=beneficiary,
        budget_options=budget_options,
    )
