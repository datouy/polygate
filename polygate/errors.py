"""异常与重试策略。

错误语义分三层：

* ``UpstreamError`` —— 内部异常，携带上游状态码、原始 body 与限流响应头。
* ``GatewayError``   —— 面向客户端的异常，渲染成 OpenAI 兼容错误体。
* 判定函数          —— 决定「换 Key / 换 Provider / 原样回传」。

关键约定：
* 上游 4xx（401/403/404/422 等，429 除外）是「调用方自己的问题」，
  换 Key 或换 Provider 都无济于事，必须原样回传状态码与响应体，
  否则 OpenAI SDK 会把参数错误当成网关故障并盲目重试。
* 上游 429 是 Key 额度维度问题：换 Key 有意义，同时也要计入 Provider 熔断。
* 连接错误 / 5xx 才是 Provider 维度问题：换 Provider，全部失败才回 502。
"""

from __future__ import annotations

# 上游响应头中需要透传给客户端的限流/排障信息
_PASSTHROUGH_HEADER_NAMES = (
    "retry-after",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "ratelimit-limit-tokens",
    "ratelimit-remaining-tokens",
    "ratelimit-reset-tokens",
)


def default_error_type(status: int | None) -> str:
    """按状态码映射到 OpenAI 约定的 ``error.type``。"""
    if status is None:
        return "api_error"
    if status == 400:
        return "invalid_request_error"
    if status in (401, 403):
        return "authentication_error"
    if status == 404:
        return "not_found_error"
    if status == 422:
        return "invalid_request_error"
    if status == 429:
        return "rate_limit_error"
    if status in (502, 503, 504):
        return "service_unavailable"
    if status >= 500:
        return "api_error"
    return "invalid_request_error"


class UpstreamError(Exception):
    """上游模型调用失败（连接错误或非 2xx 响应）。

    ``body`` 保存上游原始响应体（bytes），``headers`` 保存上游响应头
    （仅限流/排障相关），二者都会在错误路径透传给客户端。
    """

    def __init__(
        self,
        status: int | None,
        message: str,
        body: bytes | None = None,
        headers: dict | None = None,
    ):
        self.status = status
        self.message = message
        self.body = body
        self.headers: dict = dict(headers or {})
        super().__init__(message)


class GatewayError(Exception):
    """面向客户端的网关错误。

    渲染后是 OpenAI 兼容的 ``{"error": {...}}`` 结构，状态码沿用上游语义，
    并带 ``X-Request-ID`` 与上游的限流头。
    """

    def __init__(
        self,
        status_code: int,
        message: str,
        *,
        body: bytes | None = None,
        headers: dict | None = None,
        error_type: str | None = None,
        code: str | None = None,
    ):
        self.status_code = int(status_code)
        self.message = message
        self.body = body
        self.headers: dict = dict(headers or {})
        self.error_type = error_type or default_error_type(self.status_code)
        self.code = code
        super().__init__(message)


def extract_passthrough_headers(headers) -> dict:
    """从上游响应头中挑出需要透传的限流/排障头（小写键名）。"""
    out: dict = {}
    try:
        items = headers.items()
    except AttributeError:
        return out
    for name, value in items:
        if name.lower() in _PASSTHROUGH_HEADER_NAMES:
            out[name.lower()] = value
    return out


def is_retryable(status: int | None) -> bool:
    """连接错误(None)、429、5xx 视为可重试/可故障切换；其余 4xx 不重试。"""
    return status is None or status >= 500 or status == 429


def is_key_error(status: int | None) -> bool:
    """401/403 属于 Key 维度问题：应切换下一个 Key 再试。

    注意 429 不在此列：429 表示「当前 Key/额度被限流」，换 Key 有意义，
    但同时也是 Provider 维度的容量信号，必须计入熔断（见 ``is_rate_limited``）。
    """
    return status in (401, 403)


def is_rate_limited(status: int | None) -> bool:
    """429：上游限流。既要换 Key 重试，也要计入熔断失败计数。"""
    return status == 429


def is_client_error(status: int | None) -> bool:
    """上游返回 4xx（客户端参数问题）：换 Key 或 Provider 都无济于事，
    应原样把状态码返回给客户端，而不是统一降级成 502。

    429（限流）与 401/403（鉴权）被排除：它们属于 Key 维度，换 Key 可能有意义，
    由 ``is_rate_limited`` / ``is_key_error`` 单独处理。
    """
    return status is not None and 400 <= status < 500 and status not in (401, 403, 429)
