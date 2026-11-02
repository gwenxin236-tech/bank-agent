"""可选的大模型结构化意图适配器。

安全边界：
- 默认离线；仅 BANK_AGENT_OFFLINE=0 且配置完整时启用，由规则解析器兜底；
- 大模型只负责意图识别和实体抽取，不获得银行工具权限；
- 输出必须是受限 JSON，且仍由服务端 PolicyEngine 重新计算风险；
- 仅发送用户文本，不附带账户查询结果；仅限虚构演示，真实文本外发需另行脱敏审查。

兼容 OpenAI Chat Completions 风格的 HTTP API，也可通过 LLM_BASE_URL 指向
企业内部网关或其他兼容服务。
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
from collections import deque
import socket
import http.client
from contextvars import ContextVar
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit
from dataclasses import dataclass, field
from typing import Any, Dict, Optional, Tuple


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_request(request, timeout):
    # An upstream redirect must never carry the credential to another URL.
    return urllib.request.build_opener(_NoRedirect()).open(request, timeout=timeout)


def _strict_json(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    def constant(_):
        raise ValueError('non-finite JSON number')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


@dataclass(frozen=True)
class LLMConfig:
    provider: str = ""
    api_key: str = field(default="", repr=False)
    model: str = ""
    base_url: str = "https://api.openai.com/v1"
    timeout_seconds: float = 8.0
    config_error: str = ""
    offline: bool = False
    max_output_tokens: int = 512
    calls_per_minute: int = 20
    max_concurrent: int = 2

    @classmethod
    def from_env(cls) -> "LLMConfig":
        error = ""
        try:
            timeout = float(os.getenv("LLM_TIMEOUT_SECONDS", "8"))
            if not math.isfinite(timeout) or not 0 < timeout <= 60:
                raise ValueError("invalid timeout")
        except (ValueError, OverflowError):
            timeout, error = 8.0, "invalid_timeout"
        provider = os.getenv("LLM_PROVIDER", "").strip().lower()
        default_base = "https://api.siliconflow.cn/v1" if provider == "siliconflow" else "https://api.openai.com/v1"
        limits = {}
        for name, default, maximum in (("MAX_OUTPUT_TOKENS", 512, 2048),
                                        ("CALLS_PER_MINUTE", 20, 600), ("MAX_CONCURRENT", 2, 16)):
            try:
                value = int(os.getenv("LLM_" + name, str(default)))
                if not 1 <= value <= maximum:
                    raise ValueError('invalid bound')
            except (ValueError, OverflowError):
                value, error = default, "invalid_budget"
            limits[name.lower()] = value
        return cls(
            provider=provider,
            api_key=os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")).strip(),
            model=os.getenv("LLM_MODEL", os.getenv("OPENAI_MODEL", "")).strip(),
            base_url=os.getenv("LLM_BASE_URL", os.getenv("OPENAI_BASE_URL", default_base)).strip().rstrip("/"),
            timeout_seconds=timeout, config_error=error,
            offline=os.getenv("BANK_AGENT_OFFLINE", "1") != "0", **limits,
        )

    @property
    def invalid(self) -> bool:
        try:
            url = urlsplit(self.base_url)
            return bool(self.config_error or not math.isfinite(self.timeout_seconds) or
                        not 0 < self.timeout_seconds <= 60 or
                        url.scheme not in {"http", "https"} or not url.hostname or
                        (url.scheme == "http" and url.hostname not in {"localhost", "127.0.0.1", "::1"}) or
                        (self.provider.lower() == "siliconflow" and
                         (url.scheme != "https" or url.hostname != "api.siliconflow.cn" or
                          url.port not in {None, 443} or url.path != "/v1")) or
                        any(type(value) is not int or not 1 <= value <= maximum for value, maximum in
                            ((self.max_output_tokens, 2048), (self.calls_per_minute, 600), (self.max_concurrent, 16))) or
                        url.username or url.password or url.query or url.fragment or
                        (url.port is not None and not 0 < url.port <= 65535) or
                        any(c in self.api_key + self.base_url + self.model for c in "\r\n"))
        except (ValueError, TypeError, OverflowError):
            return True

    @property
    def enabled(self) -> bool:
        return bool(self.api_key and self.model and self.base_url and not self.invalid and not self.offline)

    @property
    def display_name(self) -> str:
        if not self.enabled:
            return "规则解析模式"
        return f"{self.provider or '兼容 API'} · {self.model}"


class ChatCompletionClient:
    """Bounded JSON transport, reusable by future model consumers. No tools or retries.

    Limits apply to this client instance in this process, not an account-wide quota.
    Callers retain responsibility for input privacy and application schema validation.
    """
    def __init__(self, config):
        self.config = config
        self._lock = threading.Lock()
        self._calls = deque()
        self._inflight = 0
        self._metrics = ContextVar('model_metrics', default=None)

    @property
    def metrics(self):
        return dict(self._metrics.get() or {})

    def complete_json(self, system, text):
        self._metrics.set({})
        if not self.config.enabled:
            return None, 'offline' if self.config.offline else 'invalid_config' if self.config.invalid else 'disabled'
        if not isinstance(system, str) or not isinstance(text, str) or not system or not 0 < len(text) <= 2000:
            return None, 'invalid_input'
        # Conservative text gate, not a complete DLP or production certification.
        if re.search(r'(?<!\d)1[3-9]\d{9}(?!\d)|(?<!\d)\d{15,19}[\dXx]?(?!\d)|sk-[A-Za-z0-9_-]{12,}|'
                     r'(?:验证码|动态口令|密码|口令|password|otp)\s*(?:为|是|[:：=])?\s*[A-Za-z0-9]{4,}', text, re.I):
            return None, 'privacy_blocked'
        with self._lock:
            now = time.monotonic()
            while self._calls and self._calls[0] <= now - 60:
                self._calls.popleft()
            if self._inflight >= self.config.max_concurrent:
                return None, 'concurrency_limit'
            if len(self._calls) >= self.config.calls_per_minute:
                return None, 'rate_limited'
            self._calls.append(now)
            self._inflight += 1
        started = time.monotonic()
        metrics = {'attempted': True, 'http_status': None}
        reason = 'invalid_response'
        try:
            payload = {'model': self.config.model, 'temperature': 0, 'stream': False,
                       'max_tokens': self.config.max_output_tokens,
                       'response_format': {'type': 'json_object'},
                       'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': text}]}
            if self.config.provider.lower() == 'siliconflow':
                payload['enable_thinking'] = False
            request = urllib.request.Request(self.config.base_url + '/chat/completions',
                data=json.dumps(payload, ensure_ascii=False).encode('utf-8'), method='POST',
                headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + self.config.api_key})
            with _open_request(request, timeout=self.config.timeout_seconds) as response:
                metrics['http_status'] = getattr(response, 'status', None)
                raw = response.read(65537)
            if len(raw) > 65536:
                reason = 'response_too_large'
                return None, reason
            try:
                body = _strict_json(raw.decode('utf-8'))
            except (ValueError, UnicodeError, RecursionError):
                reason = 'invalid_json'
                return None, reason
            usage = body.get('usage', {})
            if isinstance(usage, dict):
                metrics['usage'] = {k: usage[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
                                    if type(usage.get(k)) is int and 0 <= usage[k] <= 100_000_000}
            choice = body['choices'][0]
            if choice.get('finish_reason') not in {None, 'stop'}:
                reason = 'incomplete_response'
                return None, reason
            message = choice['message']
            content = message['content']
            if (message.get('role') not in {None, 'assistant'} or message.get('tool_calls') or
                    message.get('function_call') or not isinstance(content, str)):
                return None, reason
            try:
                value = _strict_json(content)
            except (ValueError, UnicodeError, RecursionError):
                reason = 'invalid_json'
                return None, reason
            reason = 'accepted'
            return value, reason
        except urllib.error.HTTPError as exc:
            metrics['http_status'] = exc.code
            reason = 'http_error'
            if 300 <= exc.code < 400:
                metrics['error_kind'] = 'redirect_blocked'
            exc.close()
        except (TimeoutError, socket.timeout, urllib.error.URLError) as exc:
            reason = 'timeout' if isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(getattr(exc, 'reason', None), (TimeoutError, socket.timeout)) else 'network_error'
        except (OSError, http.client.HTTPException):
            reason = 'network_error'
        except (KeyError, IndexError, TypeError, ValueError, AttributeError, OverflowError, RecursionError):
            reason = 'invalid_response'
        finally:
            metrics['elapsed_ms'] = max(0, int((time.monotonic() - started) * 1000))
            if reason != 'accepted':
                metrics.setdefault('error_kind', reason)
            self._metrics.set(metrics)
            with self._lock:
                self._inflight -= 1
        return None, reason


class LLMIntentAdapter:
    """把自然语言映射到受限的银行意图协议，不承担业务执行。"""

    ALLOWED_INTENTS = {
        "balance_query", "transaction_query", "bill_analysis", "investment_query",
        "subscription_query", "subscription_cancel", "transfer", "card_manage",
        "fraud_guard", "financial_health", "family_co_management", "unknown",
    }

    SYSTEM_PROMPT = """你是银行智能体的意图识别器，不是银行操作执行器。
只做两件事：从用户文本中识别 intent，并抽取必要 entities。
只能输出一个 JSON 对象，不要 Markdown，不要解释，不要调用工具。
JSON Schema：
{"intent":"balance_query|transaction_query|bill_analysis|investment_query|subscription_query|subscription_cancel|transfer|card_manage|fraud_guard|financial_health|family_co_management|unknown",
"entities":{"beneficiary":string|null,"amount_cents":integer|null,"note":string,"subscription_id":string|null,"action":string|null,"goal_name":string|null,"target_cents":integer|null,"monthly_cents":integer|null,"member":string|null,"allowance_cents":integer|null,"participants":array|null}}
规则：金额必须转换为人民币分的整数；无法确定就填 null；不要猜测收款人、金额或订阅 ID；不要输出 risk、permission、tool、transaction_id 等字段。
"""

    def __init__(self, config: Optional[LLMConfig] = None):
        self.config = config or LLMConfig.from_env()
        self.client = ChatCompletionClient(self.config)
        # Per execution context, not shared mutable "last call" state across HTTP threads.
        self._evidence = ContextVar("llm_evidence", default=None)

    @property
    def evidence(self) -> Dict[str, Any]:
        return dict(self._evidence.get() or {"source": "rules", "attempted": False,
                                            "accepted": False, "reason": "not_requested"})

    def reset_evidence(self, reason="local_priority") -> None:
        self._evidence.set({"source": "rules", "attempted": False, "accepted": False, "reason": reason})

    def _record(self, reason, attempted=True, accepted=False, metrics=None):
        self._evidence.set({"source": "llm" if accepted else "rules", "attempted": attempted,
                            "accepted": accepted, "reason": reason,
                            **{k: v for k, v in (metrics or {}).items() if k != 'attempted'}})

    def parse(self, text: str) -> Optional[Tuple[str, Dict[str, Any]]]:
        value, reason = self.client.complete_json(self.SYSTEM_PROMPT, text)
        metrics = self.client.metrics
        result = self._validate(value) if reason == 'accepted' else None
        if reason == 'accepted' and result is None:
            reason = 'schema_rejected'
        self._record(reason, attempted=metrics.get('attempted', False),
                     accepted=result is not None, metrics=metrics)
        return result

    def _validate(self, value: Any) -> Optional[Tuple[str, Dict[str, Any]]]:
        if not isinstance(value, dict):
            return None
        intent = value.get("intent")
        if not isinstance(intent, str) or intent not in self.ALLOWED_INTENTS:
            return None
        raw = value.get("entities")
        if not isinstance(raw, dict):
            return None
        for key, limit in (("beneficiary", 32), ("subscription_id", 32), ("action", 32),
                           ("goal_name", 32), ("member", 32), ("note", 120)):
            value = raw.get(key)
            if value is not None and (not isinstance(value, str) or len(value) > limit):
                return None
        participants = raw.get("participants")
        if participants is not None and (not isinstance(participants, list) or len(participants) > 8 or
                any(not isinstance(x, str) or len(x) > 16 for x in participants)):
            return None
        for key in ("amount_cents", "target_cents", "monthly_cents", "allowance_cents"):
            if raw.get(key) is not None and self._positive_int_or_none(raw[key]) is None:
                return None
        entities: Dict[str, Any] = {
            "beneficiary": self._text_or_none(raw.get("beneficiary")),
            "amount_cents": self._positive_int_or_none(raw.get("amount_cents")),
            "note": self._text_or_empty(raw.get("note"), 120),
            "subscription_id": self._text_or_none(raw.get("subscription_id")),
            "action": self._text_or_none(raw.get("action")),
            "goal_name": self._text_or_none(raw.get("goal_name")),
            "target_cents": self._positive_int_or_none(raw.get("target_cents")),
            "monthly_cents": self._positive_int_or_none(raw.get("monthly_cents")),
            "member": self._text_or_none(raw.get("member")),
            "allowance_cents": self._positive_int_or_none(raw.get("allowance_cents")),
            "participants": [x.strip() for x in participants] if participants is not None else [],
        }
        # 不允许模型改变内部事实协议；仅保留 IntentParser 需要的字段。
        return intent, entities

    @staticmethod
    def _text_or_none(value: Any) -> Optional[str]:
        if value is None:
            return None
        value = value.strip()
        return value[:32] if value else None

    @staticmethod
    def _text_or_empty(value: Any, limit: int) -> str:
        if value is None:
            return ""
        return value.strip()[:limit]

    @staticmethod
    def _positive_int_or_none(value: Any) -> Optional[int]:
        # 不允许 bool/float/字符串被隐式转成金额整数，避免 0.9、True 等绕过 Schema。
        if type(value) is not int or value <= 0 or value > 1_000_000_000_000:
            return None
        return value
