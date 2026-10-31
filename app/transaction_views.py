"""用户可见的交易与任务回执视图层。

生产替换点：真实核心/支付网关的状态码由 Adapter 归一化后再进入本模块；
本模块不改变账务状态、不授予权限，也不触发重试或副作用。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Dict, Optional


_STATUS_TEXT = {
    "processing": ("PROCESSING", "处理中", "银行还在确认这笔交易，请不要重复操作"),
    "unknown": ("UNKNOWN", "结果未知", "结果未知，正在核验这笔交易，请不要重复操作"),
    "succeeded": ("SUCCESS", "已完成", "这笔交易已完成。"),
    "success": ("SUCCESS", "已完成", "这笔交易已完成。"),
    "failed": ("FAILED", "明确失败", "银行已明确返回失败，这笔交易未完成。"),
    "reversed": ("REVERSED", "已冲正", "这笔交易已冲正，请以银行账务记录为准。"),
    "partial": ("PARTIAL", "部分完成", "这项任务部分完成，请查看逐项回执或联系人工。"),
}

# Only these tools represent a business-side effect for the aggregate label.
# Read-only checks and the human gate can be completed before a later business
# action fails; they must not turn a fully failed task into PARTIAL.
_SIDE_EFFECT_TOOLS = frozenset({
    "create_transfer", "cancel_subscription", "create_cooling_off",
    "notify_guardian", "set_family_allowance", "create_birthday_linkage",
    "lock_subaccount", "order_flowers", "order_cake",
    "compensate_subaccount_lock",
})


_TOOL_LABELS = {
    "create_transfer": "转账",
    "check_transfer_constraints": "转账条件检查",
    "create_birthday_linkage": "创建生日联动",
    "lock_subaccount": "锁定生日预算",
    "order_flowers": "下单鲜花",
    "order_cake": "下单蛋糕",
    "compensate_subaccount_lock": "补偿生日预算锁定",
    "human_gate": "人工确认",
}

_ITEM_TEXT = {
    "COMPLETED": "已完成",
    "PENDING_VERIFICATION": "已执行待核验",
    "FAILED": "明确失败",
    "NOT_EXECUTED": "未执行",
    "COMPENSATED": "已完成补偿",
    "COMPENSATION_PENDING_VERIFICATION": "补偿待核验",
}


def transaction_view(transaction: Optional[Dict[str, Any]], trace_id: str = "", outcome_unknown: bool = False) -> Dict[str, Any]:
    """把核心事实转换为用户回执；不会修改输入字典。"""
    source = deepcopy(transaction) if isinstance(transaction, dict) else {}
    raw_status = str(source.get("status", "unknown")).lower()
    key = "unknown" if outcome_unknown else raw_status
    status, title, message = _STATUS_TEXT.get(key, _STATUS_TEXT["unknown"])
    if status == "FAILED" and source.get("error_message"):
        message = str(source["error_message"])
    if status == "UNKNOWN" and source.get("error_message"):
        # Transport errors are deliberately rendered without raw exception text.
        message = "结果未知，正在核验这笔交易，请不要重复操作"
    return {
        "ok": status not in {"FAILED"},
        "status": status,
        "title": title,
        "user_message": message,
        "trace_id": trace_id,
        "core_status": raw_status,
        "needs_verification": status in {"UNKNOWN", "PROCESSING"},
        "transaction_id": source.get("transaction_id"),
    }


def is_unknown(transaction: Optional[Dict[str, Any]], outcome_unknown: bool = False) -> bool:
    return bool(outcome_unknown or (isinstance(transaction, dict) and str(transaction.get("status", "")).lower() == "unknown"))


def _as_dict(value: Any) -> Dict[str, Any]:
    if isinstance(value, dict):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        converted = to_dict()
        if isinstance(converted, dict):
            return converted
    return {}


def _execution_output(node: Dict[str, Any]) -> Dict[str, Any]:
    result = node.get("result") if isinstance(node.get("result"), dict) else {}
    execution = result.get("execution") if isinstance(result.get("execution"), dict) else result
    output = execution.get("output") if isinstance(execution.get("output"), dict) else execution
    return output if isinstance(output, dict) else {}


def _node_label(node: Dict[str, Any]) -> str:
    tool = str(node.get("tool") or node.get("node_type") or "任务步骤")
    return _TOOL_LABELS.get(tool, tool)


def _node_item(node: Dict[str, Any], dag_status: str) -> Dict[str, Any]:
    node_status = str(node.get("status", "pending")).lower()
    output = _execution_output(node)
    is_ambiguous = bool(
        output.get("outcome_unknown")
        or str(output.get("status", "")).upper() in {"UNKNOWN", "PROCESSING"}
        or str(output.get("error_code", "")).upper() in {"TOOL_OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"}
    )
    if node_status in {"succeeded", "compensated"}:
        status = "COMPLETED"
        detail = "银行/工具已返回该步骤结果。"
    elif node_status == "running" and str(node.get("node_type", "action")) == "human_gate":
        status = "NOT_EXECUTED"
        detail = "正在等待人工确认，尚未执行后续业务动作。"
    elif node_status == "running":
        # A persisted running action is conservatively treated as in-flight.
        # The executor may have crashed before the adapter returned, so the
        # receipt must ask for reconciliation rather than claim no effect.
        status = "PENDING_VERIFICATION"
        detail = "已发起但尚未取得明确结果，只能查询原请求。"
    elif node_status == "failed":
        status = "FAILED"
        detail = "该步骤明确失败，未将其伪装成成功。"
    elif node_status == "running":
        status = "NOT_EXECUTED"
        detail = "该步骤尚未形成可确认的执行结果。"
    elif node_status == "ready":
        status = "NOT_EXECUTED"
        detail = "已具备执行条件，但尚未执行。"
    else:
        status = "NOT_EXECUTED"
        detail = "该步骤未执行。" if node_status != "skipped" else "该步骤已跳过。"
    return {
        "node_id": str(node.get("node_id", "")),
        "tool": str(node.get("tool") or node.get("node_type") or ""),
        "label": _node_label(node),
        "status": status,
        "status_label": _ITEM_TEXT[status],
        "detail": detail,
    }


def _compensation_item(node: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    result = node.get("result") if isinstance(node.get("result"), dict) else {}
    compensation = result.get("compensation")
    if not isinstance(compensation, dict):
        return None
    raw = str(compensation.get("status", "")).lower()
    if raw == "succeeded":
        status = "COMPENSATED"
        detail = "声明的补偿动作已完成。"
    elif raw in {"running", "unknown", "processing"}:
        status = "COMPENSATION_PENDING_VERIFICATION"
        detail = "补偿动作已发起但结果待核验。"
    else:
        status = "FAILED"
        detail = "补偿动作未明确完成，需要人工核实。"
    tool = (compensation.get("result") or {}).get("tool") if isinstance(compensation.get("result"), dict) else None
    tool = tool or (node.get("compensation") or {}).get("tool") or "compensation"
    return {
        "node_id": str(node.get("node_id", "")),
        "tool": str(tool),
        "label": _TOOL_LABELS.get(str(tool), "补偿动作"),
        "status": status,
        "status_label": _ITEM_TEXT[status],
        "detail": detail,
    }


def dag_receipt(dag: Any) -> Dict[str, Any]:
    """只读取 DAG 持久化快照，生成逐节点用户回执，不驱动调度。"""
    source = _as_dict(dag)
    nodes = source.get("nodes") if isinstance(source.get("nodes"), list) else []
    items = [_node_item(node, str(source.get("status", ""))) for node in nodes if isinstance(node, dict)]
    compensations = [item for node in nodes if isinstance(node, dict)
                     for item in [_compensation_item(node)] if item is not None]
    statuses = {item["status"] for item in items}
    has_completed = "COMPLETED" in statuses
    has_completed_effect = any(
        item["status"] == "COMPLETED"
        and str(item.get("tool", "")) in _SIDE_EFFECT_TOOLS
        for item in items
    )
    pending_nodes = [node for node in nodes if isinstance(node, dict)
                     and str(node.get("status", "")).lower() == "running"
                     and str(node.get("node_type", "action")) != "human_gate"]
    pending_outputs = [_execution_output(node) for node in pending_nodes]
    has_processing = any(str(output.get("status", "")).upper() == "PROCESSING"
                         for output in pending_outputs)
    has_unknown = any(
        output.get("outcome_unknown")
        or str(output.get("status", "")).upper() in {"UNKNOWN", "RESULT_UNKNOWN"}
        or str(output.get("error_code", "")).upper() in {"TOOL_OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"}
        for output in pending_outputs
    )
    has_unresolved = bool(pending_nodes) and not has_processing and not has_unknown
    compensation_pending = any(item["status"] == "COMPENSATION_PENDING_VERIFICATION"
                               for item in compensations)
    has_pending = has_processing or has_unknown or has_unresolved or compensation_pending
    has_failed = "FAILED" in statuses or "FAILED" in {item["status"] for item in compensations}
    has_unexecuted = "NOT_EXECUTED" in statuses
    dag_status = str(source.get("status", "")).lower()
    if dag_status == "completed" and not has_pending and not has_failed and not has_unexecuted:
        overall = "SUCCESS"
    elif has_unknown or has_unresolved or compensation_pending:
        overall = "UNKNOWN"
    elif has_processing:
        overall = "PROCESSING"
    elif has_completed_effect and (has_failed or has_unexecuted or dag_status in {"failed", "cancelled", "handoff"}):
        overall = "PARTIAL"
    elif dag_status in {"failed", "cancelled"} or has_failed:
        overall = "FAILED"
    elif dag_status in {"running", "paused", "waiting_human"}:
        overall = "PROCESSING"
    else:
        overall = "PARTIAL" if has_completed else "FAILED"
    return {
        "status": overall,
        "status_label": {"SUCCESS": "已完成", "UNKNOWN": "结果未知", "PARTIAL": "部分完成",
                          "FAILED": "明确失败", "PROCESSING": "处理中"}.get(overall, "请核验"),
        "items": items,
        "compensations": compensations,
        "dag_status": dag_status,
    }


def receipt_view(receipt: Dict[str, Any], trace_id: str = "") -> Dict[str, Any]:
    """把逐项回执包装成现有 result_view 契约。"""
    source = deepcopy(receipt) if isinstance(receipt, dict) else {"status": "FAILED", "items": []}
    status = str(source.get("status", "FAILED")).upper()
    messages = {
        "SUCCESS": (True, "已完成", "这项任务已完成。"),
        "PROCESSING": (True, "银行正在确认", "银行还在确认这笔交易，请不要重复操作"),
        "UNKNOWN": (True, "结果未知", "结果未知，正在核验这笔交易，请不要重复操作"),
        "PARTIAL": (False, "部分完成", "这项任务部分完成，请查看逐项回执；未执行步骤不会被冒充为已完成。"),
        "FAILED": (False, "明确失败", "这项任务明确失败，请查看逐项回执；已完成步骤仍以回执为准。"),
    }
    ok, title, message = messages.get(status, messages["FAILED"])
    source["trace_id"] = trace_id
    source["status"] = status
    return {"ok": ok, "status": status, "title": title, "user_message": message,
            "trace_id": trace_id, "receipt": source}
