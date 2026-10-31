"""Trusted feature-tool ABI. Models supply parameters, never authorization.

D4 exposes no bank, database, session token or mutable runtime to domain tools.
This is an in-process collaboration boundary, NOT a sandbox for hostile Python.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict


@dataclass(frozen=True)
class ToolContext:
    trace_id: str
    task_id: str
    node_id: str
    actor_id: str
    subject_id: str
    device_id: str
    authorized: bool


class BaseTool(ABC):
    name: str
    authz_required: bool = True
    side_effect: bool = False
    title: str = '域示例工具'
    # Closed parameter schema: "text" or non-negative integer "cents".
    parameters: Dict[str, str] = {}

    @abstractmethod
    def execute(self, ctx: ToolContext, **params: Any) -> dict:
        """Return {ok: bool, data: dict, error_code?: str, error_message?: str}.

        Use *_cents integers for money. Never determine permissions from params.
        No core access, automatic transfer or side-effect retries in D4 examples.
        """
        raise NotImplementedError


def validate_json(value, key=''):
    """Small JSON-only ABI: floats forbidden; monetary values are integer cents."""
    if key.endswith('_cents') and type(value) is not int:
        raise ValueError('金额必须是整数分，不能使用布尔值、浮点数或字符串')
    if type(value) is dict:
        for name, item in value.items():
            if type(name) is not str:
                raise ValueError('结构化字段名必须是字符串')
            validate_json(item, name)
    elif type(value) is list:
        for item in value:
            validate_json(item)
    elif type(value) not in (str, int, bool, type(None)):
        raise ValueError('工具契约只接受 JSON 基本类型，禁止浮点数')


def validate_params(schema, params):
    if type(params) is not dict or set(params) != set(schema):
        raise ValueError('参数字段与工具契约不符；不能通过参数声明身份或权限')
    validate_json(params)
    for name, kind in schema.items():
        value = params[name]
        if kind == 'cents' and (type(value) is not int or value < 0):
            raise ValueError('金额必须是非负整数分')
        if kind == 'text' and (type(value) is not str or not value.strip() or len(value) > 500):
            raise ValueError('文本参数必须是 1 至 500 个字符')


def validate_result(result):
    if (type(result) is not dict or type(result.get('ok')) is not bool or
            type(result.get('data')) is not dict or
            set(result) - {'ok', 'data', 'error_code', 'error_message'}):
        raise ValueError('工具必须返回结构化结果，不得伪造工具名或 trace')
    validate_json(result)
    if result['ok']:
        if not isinstance(result['data'].get('message'), str) or not result['data']['message'].strip():
            raise ValueError('成功结果需要来自工具的可读 message')
    elif not all(type(result.get(k)) is str and result[k] for k in ('error_code', 'error_message')):
        raise ValueError('失败结果必须有错误码与原因')
