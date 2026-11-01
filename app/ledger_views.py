"""Deterministic double-口径 ledger views for the offline bank substitute.

This module is intentionally read-only.  Production replacement points are the
core ledger, card-network statement, import-normalization and funds-status
adapters.  No model output is used for arithmetic or account state.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime,timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple


_PROCESSING_STATUSES = {"created", "processing", "pending", "unknown", "result_unknown"}
_RESTRICTED_STATUSES = {"frozen", "restricted", "reported_loss", "受限", "冻结"}
_EXPECTED_STATUSES = {"expected", "expected_arrival", "预计到账"}
_TRANSFER_TYPES = {"transfer", "internal_transfer", "本人转账", "转账", "internal"}
_REPAYMENT_TYPES = {"repayment", "credit_card_repayment", "credit_card_payment", "card_repayment", "还款", "信用卡还款"}
_REFUND_TYPES = {"refund", "退款"}
_INCOME_CATEGORIES = {"收入", "工资", "工资入账", "利息", "退款", "返现"}


def _is_int(value: Any) -> bool:
    return type(value) is int


def _amount(item: Dict[str, Any]) -> int:
    value = item.get("amount_cents")
    if not _is_int(value):
        raise TypeError("ledger amount_cents must be an integer")
    return value


def _kind(item: Dict[str, Any]) -> str:
    return str(item.get("transaction_type") or item.get("type") or item.get("category") or "").strip().lower()


def _status(item: Dict[str, Any]) -> str:
    return str(item.get("status") or "succeeded").strip().lower()


def _date_key(item: Dict[str, Any]) -> Optional[str]:
    # Posted/settled date is the accounting date; the event date is fallback.
    value = item.get("posted_date") or item.get("settled_date") or item.get("date")
    return str(value)[:10] if value else None


def _in_period(item: Dict[str, Any], period_start: Optional[str], period_end: Optional[str]) -> bool:
    key = _date_key(item)
    if key is None:
        return False if period_start or period_end else True
    return not ((period_start and key < period_start) or (period_end and key > period_end))


def _dedup_key(item: Dict[str, Any]) -> Optional[Tuple[str, str]]:
    # Only explicit source linkage is safe.  Matching merchant/amount/date alone
    # is deliberately not treated as the same transaction.
    for field in ("canonical_id", "original_transaction_id", "linked_transaction_id"):
        value = item.get(field)
        if value:
            return field, str(value)
    return None


def _is_processing(item: Dict[str, Any]) -> bool:
    return _status(item) in _PROCESSING_STATUSES


def _is_restricted(item: Dict[str, Any]) -> bool:
    return _status(item) in _RESTRICTED_STATUSES or str(item.get("funds_state", "")).lower() in _RESTRICTED_STATUSES


def _is_expected(item: Dict[str, Any]) -> bool:
    return _status(item) in _EXPECTED_STATUSES or str(item.get("funds_state", "")).lower() in _EXPECTED_STATUSES


def _is_transfer(item: Dict[str, Any]) -> bool:
    kind = _kind(item)
    return kind in _TRANSFER_TYPES or str(item.get("category", "")).strip() in _TRANSFER_TYPES


def _is_repayment(item: Dict[str, Any]) -> bool:
    kind = _kind(item)
    return kind in _REPAYMENT_TYPES or str(item.get("category", "")).strip() in _REPAYMENT_TYPES


def _is_refund(item: Dict[str, Any]) -> bool:
    kind = _kind(item)
    return kind in _REFUND_TYPES or str(item.get("category", "")).strip() in _REFUND_TYPES


def _is_consumption(item: Dict[str, Any]) -> bool:
    amount = _amount(item)
    if amount >= 0 or _is_transfer(item) or _is_repayment(item) or _is_refund(item):
        return False
    return str(item.get("category", "")).strip() not in _INCOME_CATEGORIES


def _copy_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return deepcopy(item)


def _unique_completed(items: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    result: List[Dict[str, Any]] = []
    seen = set()
    for item in items:
        key = _dedup_key(item)
        if key is not None:
            if key in seen:
                continue
            seen.add(key)
        result.append(_copy_item(item))
    return result


def _coverage(coverage: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    supplied = dict(coverage or {})
    complete = supplied.get("complete") is True
    scope = supplied.get("scope") or "当前账户/演示账户"
    updated_at = supplied.get("updated_at") or "以本次查询时间为准"
    return {
        "scope": scope,
        "complete": complete,
        "updated_at": updated_at,
        "message": supplied.get("message") or ("已覆盖全部纳入范围的账户" if complete else f"当前仅覆盖{scope}，不代表全部资产；更新时间：{updated_at}"),
    }


def apply_category_correction(item: Dict[str, Any], corrected_category: str, corrected_by: str = "user", corrected_at: Optional[str] = None) -> Dict[str, Any]:
    """Return a corrected copy while preserving the original imported category."""
    if not isinstance(corrected_category, str) or not corrected_category.strip():
        raise ValueError("corrected_category must be non-empty")
    corrected = _copy_item(item)
    original = item.get("category")
    corrected["corrected_category"] = corrected_category.strip()
    corrected["correction_record"] = {
        "original_category": original,
        "corrected_category": corrected["corrected_category"],
        "corrected_by": corrected_by,
        "corrected_at": corrected_at or "未提供",
    }
    return corrected


def build_ledger_views(
    transactions: Iterable[Dict[str, Any]],
    period_start: Optional[str] = None,
    period_end: Optional[str] = None,
    balances: Optional[Dict[str, Any]] = None,
    coverage: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Build consumer, cash-flow and funds-status views using integer cents.

    `transactions` is assumed to be the authoritative, already-authorized
    account slice.  This function never writes, calls tools, or infers missing
    transactions.  Processing/unknown entries are reported separately rather
    than counted as completed money movement.
    """
    source_items = []
    for raw in transactions:
        item = dict(raw)
        _amount(item)  # fail closed instead of silently coercing a float/string
        if _in_period(item, period_start, period_end):
            source_items.append(item)

    settled_keys={_dedup_key(x) for x in source_items if _status(x) in {'succeeded','posted','settled','completed','success'} and _dedup_key(x)}
    processing_items = _unique_completed([item for item in source_items if _is_processing(item) and (not _dedup_key(item) or _dedup_key(item) not in settled_keys)])
    restricted_items = [item for item in source_items if _is_restricted(item)]
    expected_items = [item for item in source_items if _is_expected(item)]
    completed_items = [item for item in source_items if _status(item) in {'succeeded','posted','settled','completed','success'} and not _is_restricted(item) and not _is_expected(item)]

    # De-duplicate only explicit cross-source links, separately for each view.
    consumer_candidates = [item for item in completed_items if _is_consumption(item) or _is_refund(item)]
    consumer_items = _unique_completed(consumer_candidates)
    cash_candidates = [item for item in completed_items if _amount(item) != 0]
    cash_items = _unique_completed(cash_candidates)

    by_category: Dict[str, int] = {}
    total_cents = 0
    refund_cents = 0
    for item in consumer_items:
        amount = _amount(item)
        if _is_refund(item) or amount > 0:
            refund = abs(amount)
            refund_cents += refund
            total_cents -= refund
            continue
        spent = abs(amount)
        total_cents += spent
        category = item.get("corrected_category") or item.get("category") or "未分类"
        by_category[category] = by_category.get(category, 0) + spent

    outflow_cents = sum(abs(_amount(item)) for item in cash_items if _amount(item) < 0)
    inflow_cents = sum(_amount(item) for item in cash_items if _amount(item) > 0)
    net_outflow_cents = outflow_cents - inflow_cents

    processing_cents = sum(abs(_amount(item)) for item in processing_items if _amount(item) != 0)
    restricted_cents = sum(abs(_amount(item)) for item in restricted_items if _amount(item) != 0)
    expected_arrival_cents = sum(abs(_amount(item)) for item in expected_items if _amount(item) > 0)
    funds = dict(balances or {})
    funds_status = {
        "available_cents": funds.get("available_cents", funds.get("balance_cents")),
        "processing_cents": funds.get("processing_cents", processing_cents),
        "restricted_cents": funds.get("restricted_cents", restricted_cents),
        "expected_arrival_cents": funds.get("expected_arrival_cents", expected_arrival_cents),
        "rules": ["可用、处理中、冻结或受限、预计到账分开展示，不合并为总资产"],
    }

    # A refund without an in-period purchase cannot make the period's consumption negative; keep the refund amount explicit.
    total_cents = max(total_cents, 0)

    return {
        "consumer_view": {
            "total_cents": total_cents,
            "refund_cents": refund_cents,
            "by_category": by_category,
            "items": consumer_items,
            "rules": ["实际消费计入", "退款冲减消费", "本人转账不计入消费", "信用卡还款不计入消费"],
        },
        "cash_flow_view": {
            "outflow_cents": outflow_cents,
            "inflow_cents": inflow_cents,
            "net_outflow_cents": net_outflow_cents,
            "items": cash_items,
            "label": "包含还款，不等于消费",
            "rules": ["展示本账户已入账资金流出", "信用卡还款计入资金流出", "本人转账计入资金流出", "退款作为资金流入"],
        },
        "funds_status": funds_status,
        "coverage": _coverage(coverage),
        "period": {"start": period_start, "end": period_end, "date_basis": "posted_date 优先，其次 settled_date/date"},
        "rules": {
            "arithmetic": "服务端确定性整数分计算",
            "deduplication": "仅按明确 canonical_id/original_transaction_id/linked_transaction_id 去重，不按商户金额日期猜测",
            "projected_income": "预计收入不计入可用余额",
        },
    }


def build_funding_plan(*,available_cents,target_cents=0,reserve_cents=0,commitments=(),sources=(),deadline=None,now=None):
    """Read-only integer plan. Future inflows never become current availability."""
    for name,value in [('available',available_cents),('target',target_cents),('reserve',reserve_cents)]:
        if value is None and name=='available':continue
        if type(value) is not int or not 0<=value<=2**63-1:raise ValueError('nonnegative integer cents required')
    now=now or datetime.now(timezone.utc)
    if deadline is not None:
        instant=datetime.fromisoformat(deadline)
        if instant.tzinfo is None:raise ValueError('timezone-aware deadline required')
    seen={};conflict=False;total=0
    for item in commitments:
        if item.get('status')!='confirmed':continue
        amount=item.get('amount_cents')
        if type(amount) is not int or amount<0:conflict=True;continue
        key=item.get('id')
        if key and key in seen:
            if seen[key]!=amount:conflict=True
            continue
        if key:seen[key]=amount
        total+=amount
    future=sum(x['amount_cents'] for x in sources if type(x.get('amount_cents')) is int and x['amount_cents']>0)
    spendable=None if available_cents is None or conflict else max(0,available_cents-total-reserve_cents)
    gap=None if spendable is None else max(0,target_cents-spendable)
    status='needs_review' if gap is None else 'shortfall' if gap else 'ready_now'
    if deadline is not None and now>=instant:status='deadline_expired'
    return {'status':status,'target_cents':target_cents,'available_cents':available_cents,'confirmed_expenses_cents':total,
        'reserve_cents':reserve_cents,'spendable_now_cents':spendable,'shortfall_cents':gap,'future_unavailable_cents':future,
        'deadline':deadline,'coverage':'current_authorized_account_only','source':'ledger_views.py:build_funding_plan',
        'observed_at':now.isoformat(),'business_executed':False,'options':['review_budget','human_handoff'] if status!='ready_now' else [],
        'message':'资金准备方案只核对当前可用资金、确认支出和保留额；预计到账不计可用，不代表到账承诺，不自动借贷或赎回。'}
