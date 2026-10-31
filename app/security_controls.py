"""HTTP 层最小安全控制。

这些控制用于演示和联调基线，不能替代银行生产环境的 WAF、API Gateway、IAM、
密钥管理、风控、渗透测试、等保和合规审查。
"""
from __future__ import annotations

import threading
import time
from collections import defaultdict, deque
from typing import Deque, Dict
from policy_config import get_policy


class SlidingWindowRateLimiter:
    def __init__(self, limit: int = None, window_seconds: int = None) -> None:
        self.limit = get_policy()["http_rate_limit"] if limit is None else limit
        self.window_seconds = get_policy()["http_rate_window_seconds"] if window_seconds is None else window_seconds
        self._lock = threading.Lock()
        self._events: Dict[str, Deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        with self._lock:
            events = self._events[key]
            while events and now - events[0] > self.window_seconds:
                events.popleft()
            if len(events) >= self.limit:
                return False
            events.append(now)
            return True

    def state(self) -> Dict[str, int]:
        with self._lock:
            return {"limit": self.limit, "window_seconds": self.window_seconds, "tracked_keys": len(self._events)}
