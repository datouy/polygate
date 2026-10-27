"""异常与重试策略。"""

from __future__ import annotations

from typing import Optional


class UpstreamError(Exception):
    """上游模型调用失败（连接错误或非 2xx 响应）。"""

    def __init__(self, status: Optional[int], message: str):
        self.status = status
        self.message = message
        super().__init__(message)


def is_retryable(status: Optional[int]) -> bool:
    """连接错误(None)、429、5xx 视为可重试/可故障切换；其余 4xx 不重试。"""
    return status is None or status >= 500 or status == 429


def is_key_error(status: Optional[int]) -> bool:
    """401/403/429 属于 Key 维度问题：应切换下一个 Key 再试。"""
    return status in (401, 403, 429)
