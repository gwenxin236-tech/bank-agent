"""渠道无关的消息契约与 Agent Gateway。

本模块不连接任何第三方平台。它把 Web、App、企业 IM、飞书、钉钉等入口
统一成同一种内部消息结构，避免把银行业务逻辑写进某个渠道适配器。
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional, Protocol


@dataclass(frozen=True)
class UnifiedMessage:
    channel: str
    user_id: str
    conversation_id: str
    message_type: str = "text"
    text: str = ""
    attachments: List[Dict[str, Any]] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    message_id: str = field(default_factory=lambda: f"msg_{uuid.uuid4().hex[:12]}")
    received_at: float = field(default_factory=time.time)

    def as_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ChannelAdapter(Protocol):
    """第三方渠道只需要实现解析和发送，不实现 Agent 业务规则。"""

    channel_name: str

    def parse(self, raw: Any) -> UnifiedMessage:
        ...

    def send(self, conversation_id: str, payload: Dict[str, Any]) -> Any:
        ...


class InMemorySessionStore:
    """演示用会话映射。

    生产环境应替换为 Redis/PostgreSQL，并绑定真实登录用户、设备和授权上下文。
    """

    def __init__(self, max_sessions=1024, ttl_seconds=1800) -> None:
        if max_sessions < 1 or ttl_seconds <= 0:
            raise ValueError('Session limits must be positive')
        self.max_sessions, self.ttl_seconds = max_sessions, ttl_seconds
        self._lock = threading.Lock()
        self._sessions: Dict[tuple, Dict[str, Any]] = {}

    def get_or_create(self, channel: str, user_id: str, conversation_id: str) -> Dict[str, Any]:
        key = (channel, user_id, conversation_id)
        with self._lock:
            now = time.time()
            for old_key, old in list(self._sessions.items()):
                if now - old['last_seen_at'] >= self.ttl_seconds:
                    del self._sessions[old_key]
            state = self._sessions.get(key)
            if state is None:
                if len(self._sessions) >= self.max_sessions:
                    del self._sessions[next(iter(self._sessions))]
                state = {
                    "session_id": f"sess_{secrets.token_hex(8)}",
                    "channel": channel,
                    "user_id": user_id,
                    "conversation_id": conversation_id,
                    "created_at": time.time(),
                    "last_seen_at": time.time(),
                }
                self._sessions[key] = state
            else:
                state["last_seen_at"] = time.time()
            return dict(state)


class AgentGateway:
    """统一消息网关：渠道协议 → 内部消息 → Agent Runtime → 渠道响应。"""

    SUPPORTED_CHANNELS = {"web", "app", "feishu", "dingtalk", "wecom", "telegram", "slack", "local_demo"}

    def __init__(self, runtime: Any, sessions: Optional[InMemorySessionStore] = None) -> None:
        self.runtime = runtime
        self.sessions = sessions or InMemorySessionStore()

    def handle(self, message: UnifiedMessage) -> Dict[str, Any]:
        # Ignore client metadata trace IDs; ingress owns correlation identifiers.
        trace = "trace_" + uuid.uuid4().hex.translate(str.maketrans("0123456789abcdef", "abcdefghijklmnop"))
        text = (message.text or "").strip()[:2000]
        session = self.sessions.get_or_create(message.channel, message.user_id, message.conversation_id)
        if message.message_type != "text" or not text:
            self.runtime.audit.write(trace, "input.rejected", {"reason": "unsupported_or_empty_input"}, actor=message.user_id)
            response = {"ok": False, "trace_id": trace, "message": "目前演示版先处理文字消息，图片和文件入口已预留。" if message.message_type != "text" else "请告诉我你想办理什么业务。"}
        else:
            response = self.runtime.chat(text, actor_id=message.user_id, session_id=session["session_id"], trace_id=trace,
                                         device_id=message.metadata.get('device_id', 'demo-device'), channel=message.channel)
        response["gateway"] = {
            "channel": message.channel,
            "session_id": session["session_id"],
            "message_id": message.message_id,
            "trace_id": trace,
        }
        return response

    def capabilities(self) -> Dict[str, Any]:
        return {
            "channel_independent": True,
            "supported_contract_channels": sorted(self.SUPPORTED_CHANNELS),
            "connected_channels": ["web", "local_demo"],
            "planned_channels": ["app", "feishu", "dingtalk", "wecom", "telegram", "slack"],
            "note": "已提供 Web 与独立本地终端适配器（scripts/channel_demo.py）；local_demo 仅为本地演示，不是外部 IM 接入。其他渠道仅有契约设计。",
        }


def verify_hmac_signature(secret: str, body: bytes, signature: str, prefix: str = "sha256=") -> bool:
    """校验未来 IM Webhook 的签名，防伪造和篡改。"""
    if not secret or not signature.startswith(prefix):
        return False
    supplied = signature[len(prefix):].strip().lower()
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(supplied, expected)
