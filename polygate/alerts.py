"""告警通知：熔断 / 配额 / 预算事件推送到 Webhook（企微 / 钉钉 / Slack）。

设计原则：
* fire-and-forget：告警发送失败绝不影响业务请求（后台任务 + 全异常吞掉）。
* 降级静默：未配置 ``alerting.webhook_url`` 时所有调用都是空操作。
* 频率保护：同一事件（按 ``key`` 去重）在 ``cooldown`` 秒内只发一次，
  避免熔断抖动时轰炸群聊。
"""

from __future__ import annotations

import asyncio
import threading
import time

import httpx


class Alerter:
    def __init__(self, webhook_url: str | None = None, cooldown: float = 300.0, timeout: float = 5.0):
        self.webhook_url = (webhook_url or "").strip() or None
        self.cooldown = max(1.0, float(cooldown))
        self.timeout = max(1.0, float(timeout))
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.webhook_url)

    def notify(self, key: str, event: str, message: str, detail: dict | None = None) -> None:
        """发送一条告警。``key`` 用于去重（如 ``circuit:deepseek``）。

        同步调用、安全无异常；在有事件循环时丢给后台任务发送，
        无事件循环时（如单测）直接跳过网络发送只记录。
        """
        if not self.webhook_url:
            return
        now = time.time()
        with self._lock:
            last = self._last_sent.get(key, 0.0)
            if now - last < self.cooldown:
                return
            self._last_sent[key] = now
        payload = {
            "event": event,
            "message": message,
            "detail": detail or {},
            "ts": now,
            "source": "polygate",
        }
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        loop.create_task(self._send(payload))

    async def _send(self, payload: dict) -> None:
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                await client.post(self.webhook_url, json=payload)
        except Exception:
            pass

    def snapshot(self) -> dict:
        """供 /v1/status 展示：是否启用、去重中的事件数。"""
        with self._lock:
            return {
                "enabled": self.enabled,
                "muted_events": len(self._last_sent),
                "cooldown_sec": self.cooldown,
            }
