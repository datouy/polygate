"""FastAPI 网关服务。

统一 /v1/chat/completions、/v1/embeddings、/v1/audio/transcriptions，
支持流式透传、多 Key 轮询、权重负载均衡、故障切换、令牌桶限流（内存/Redis）、
熔断器、能力约束路由、SQLite 持久化、请求日志与多模型重放、Provider 兼容性探测、
硬预算租约、按 Key 成本记账、Prometheus 指标与管理面板，并支持在线修改配置后热重载。

错误语义约定（对客户端友好）：
* 上游 4xx（401/403/404/422 等）原样回传状态码与响应体，客户端才能区分
  「参数写错」「Key 失效」这类自身问题，而不是一律当成网关故障（502）。
* 上游 429/5xx/连接错误才触发换 Key / 换 Provider 的故障切换。
* 所有错误响应都带 ``X-Request-ID``，并透传上游 ``Retry-After`` / ``ratelimit-*``。
"""

from __future__ import annotations

import asyncio
import copy
import hmac
import json
import os
import random
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any
from urllib.parse import quote

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from . import __version__
from .admin import ADMIN_HTML, WEB_DIR
from .budget import BudgetLedger
from .clients import QuotaChecker
from .compat import CompatibilityStore
from .config import load_config
from .configstore import ConfigStore, merge_config
from .cost import CostTracker, compute_cost
from .db import BatchWriter
from .errors import (
    GatewayError,
    UpstreamError,
    default_error_type,
    is_client_error,
    is_key_error,
    is_rate_limited,
    is_retryable,
)
from .metrics import Metrics
from .ratelimit import RateLimiter
from .requestlog import RequestLog
from .router import Candidate
from .runtime import RuntimeState, mask_key
from .schemas import (
    ChatRequest,
    ClientKeyCreate,
    ClientKeyPatch,
    CompatibilityProbeRequest,
    ConfigPatch,
    EmbeddingsRequest,
    ProviderTestRequest,
    ReplayLabRequest,
)
from .secretstore import ProviderSecretStore
from .stats import Stats
from .store import UsageStore
from .virtualkeys import VirtualKeyStore, hash_client_key

# 透传给客户端的上游响应头（排除 x-request-id，它另存为 X-Upstream-Request-Id）
_PASSTHROUGH_HEADERS = (
    "retry-after",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "ratelimit-limit-tokens",
    "ratelimit-remaining-tokens",
    "ratelimit-reset-tokens",
)

# 请求体大小上限（字节）。默认 10MB，防止单个超大请求打爆内存/日志库。
MAX_REQUEST_BYTES = int(os.environ.get("POLYGATE_MAX_REQUEST_BYTES", 10 * 1024 * 1024))
MAX_AUDIO_BYTES = int(os.environ.get("POLYGATE_MAX_AUDIO_BYTES", 25 * 1024 * 1024))
MAX_ERROR_BYTES = 64 * 1024
MAX_SSE_EVENT_BYTES = 1024 * 1024


def _header_text(value: Any) -> str:
    """把可能含中文或控制字符的标识安全放入 HTTP 响应头。"""
    return quote(str(value or ""), safe="._-:")


class RequestTooLarge(Exception):
    pass


async def _bounded_error_bytes(resp: httpx.Response) -> bytes:
    out = bytearray()
    async for chunk in resp.aiter_bytes():
        out.extend(chunk[: MAX_ERROR_BYTES - len(out)])
        if len(out) >= MAX_ERROR_BYTES:
            break
    return bytes(out)


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return min(5.0, max(0.0, float(value)))
    except (TypeError, ValueError):
        return None



def _resolve_config_path(config):
    """解析配置来源：显式参数 > 环境变量 > config.yaml > config.example.yaml。"""
    if config:
        return config
    env = os.environ.get("POLYGATE_CONFIG")
    if env:
        return env
    if os.path.exists("config.yaml"):
        return "config.yaml"
    return "config.example.yaml"


def _merge_config_patch(overrides: dict, payload: ConfigPatch) -> tuple[dict, list[str]]:
    result = copy.deepcopy(overrides)
    changed: list[str] = []
    provider_updates = payload.providers
    if provider_updates:
        target = result.setdefault("providers", {})
        for name, patch in provider_updates.items():
            values = patch.model_dump(exclude_unset=True, exclude_none=True)
            if values:
                target.setdefault(name, {}).update(values)
                changed.append(name)
    if payload.budget is not None:
        values = payload.budget.model_dump(exclude_unset=True, exclude_none=True)
        if values:
            result.setdefault("budget", {}).update(values)
            changed.append("budget")
    if payload.models:
        models = result.setdefault("models", [])
        by_name = {item.get("name"): item for item in models if isinstance(item, dict) and item.get("name")}
        for model in payload.models:
            values = model.model_dump(exclude_unset=True, exclude_none=True)
            existing = by_name.get(model.name)
            if existing is None:
                existing = {"name": model.name}
                models.append(existing)
                by_name[model.name] = existing
            existing.update(values)
            changed.append(f"model:{model.name}")
    return result, changed


def _config_diff(before: dict, after: dict) -> list[dict]:
    changes: list[dict] = []
    old_providers = before.get("providers") or {}
    new_providers = after.get("providers") or {}
    for name in sorted(set(old_providers) | set(new_providers)):
        old = old_providers.get(name)
        new = new_providers.get(name)
        if old is None:
            changes.append({"kind": "provider", "title": name, "detail": "添加"})
        elif new is None:
            changes.append({"kind": "provider", "title": name, "detail": "移除"})
        else:
            fields = []
            if old.get("base_url") != new.get("base_url"):
                fields.append("Base URL")
            if old.get("timeout") != new.get("timeout"):
                fields.append("超时")
            if (old.get("key_hint"), old.get("key_count")) != (new.get("key_hint"), new.get("key_count")):
                fields.append("密钥")
            if fields:
                changes.append({"kind": "provider", "title": name, "detail": "、".join(fields)})

    old_models = {item.get("name"): item for item in (before.get("models") or []) if item.get("name")}
    new_models = {item.get("name"): item for item in (after.get("models") or []) if item.get("name")}
    for name in sorted(set(old_models) | set(new_models)):
        old = old_models.get(name)
        new = new_models.get(name)
        if old is None:
            changes.append({"kind": "model", "title": name, "detail": "添加"})
        elif new is None:
            changes.append({"kind": "model", "title": name, "detail": "移除"})
        elif old.get("routes") != new.get("routes") or old.get("kind") != new.get("kind"):
            changes.append({"kind": "model", "title": name, "detail": "路由"})
    if before.get("budget") != after.get("budget"):
        changes.append({"kind": "budget", "title": "预算", "detail": "更新"})
    return changes


def _upstream_headers(resp: httpx.Response) -> dict:
    """透传上游的排障/限流响应头，并把上游 x-request-id 改名避免与网关冲突。"""
    out: dict = {}
    if resp.headers.get("x-request-id"):
        out["X-Upstream-Request-Id"] = resp.headers["x-request-id"]
    for name in _PASSTHROUGH_HEADERS:
        if name in resp.headers:
            out[name] = resp.headers[name]
    return out


def _response_payload(response: Response, include_response: bool) -> tuple[Any, int, int]:
    """把内部上游响应转换成管理员摘要，并始终尽力提取 usage。"""
    raw = bytes(getattr(response, "body", b"") or b"")
    if not raw:
        return None, 0, 0
    try:
        data = json.loads(raw)
    except Exception:
        preview = raw[:64 * 1024].decode("utf-8", "replace")
        return (preview if include_response else None), 0, 0
    prompt, completion = _usage_from_payload(data)
    if not include_response:
        return None, prompt, completion
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    if len(encoded) > 256 * 1024:
        return {"truncated": True, "preview": encoded[:256 * 1024]}, prompt, completion
    return data, prompt, completion


def _usage_from_payload(data: Any) -> tuple[int, int]:
    if not isinstance(data, dict):
        return 0, 0
    usage = data.get("usage") or {}
    prompt = int(usage.get("prompt_tokens", usage.get("total_tokens", 0)) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    return prompt, completion


def _normalize_requirements(values) -> set[str]:
    if isinstance(values, str):
        values = values.split(",")
    if not isinstance(values, (list, tuple, set)):
        return set()
    return {str(value).strip().lower() for value in values if str(value).strip()}


def _contains_image_input(value: Any) -> bool:
    if isinstance(value, dict):
        if str(value.get("type") or "").lower() in ("image_url", "input_image"):
            return True
        return any(_contains_image_input(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_image_input(item) for item in value)
    return False


def _detected_requirements(payload: dict, explicit: set[str] | None = None) -> set[str]:
    required = set(explicit or set())
    if payload.get("stream"):
        required.add("stream")
    if payload.get("tools") or payload.get("tool_choice"):
        required.add("tools")
    response_format = payload.get("response_format")
    if isinstance(response_format, dict) and response_format.get("type") in (
        "json_object", "json_schema",
    ):
        required.add("json")
    if _contains_image_input(payload.get("messages")):
        required.add("vision")
    modalities = payload.get("modalities")
    if isinstance(modalities, list):
        normalized = {str(item).lower() for item in modalities}
        if "image" in normalized:
            required.add("vision")
        if "audio" in normalized:
            required.add("audio")
    return required


def _error_body(message: str, error_type: str, code: str | None = None) -> dict:
    """构造 OpenAI 兼容的错误体。

    用 ``{"error": {...}}`` 而不是 FastAPI 默认的 ``{"detail": ...}``，
    这样 OpenAI 官方 SDK 才能正确识别错误类型并决定是否重试。
    """
    err: dict = {"message": message, "type": error_type}
    if code:
        err["code"] = code
    return {"error": err}


def _gateway_error_response(exc: GatewayError, req_id: str | None = None) -> Response:
    """把 GatewayError 渲染成客户端响应。

    * 状态码沿用上游语义（400/401/404/429 …），而不是一律 502。
    * 若上游给了原始 body 且是 JSON，则原样回传，保留上游的细节。
    * 错误响应同样带 ``X-Request-ID``，客户端可与请求日志对齐。
    * 透传 ``Retry-After`` / ``ratelimit-*``，让客户端知道何时该退避重试。
    """
    headers = dict(exc.headers or {})
    if req_id:
        headers["X-Request-ID"] = req_id

    body = exc.body
    if body:
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        if isinstance(parsed, dict) and "error" in parsed:
            payload = parsed
        elif isinstance(parsed, dict):
            payload = _error_body(exc.message, exc.error_type, exc.code)
        else:
            payload = _error_body(exc.message, exc.error_type, exc.code)
    else:
        payload = _error_body(exc.message, exc.error_type, exc.code)

    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        status_code=exc.status_code,
        media_type="application/json",
        headers=headers,
    )


def _timeout_for(prov) -> httpx.Timeout:
    """让每个 Provider 的 timeout 配置真正生效。"""
    t = float(getattr(prov, "timeout", None) or 120.0)
    return httpx.Timeout(t, connect=min(10.0, t))


def _extract_stream_usage(line: bytes, usage: dict) -> dict:
    """从一条 SSE ``data:`` 行里提取 usage（OpenAI 兼容：usage 在末尾一次性给出）。"""
    line = line.strip()
    if not line.startswith(b"data:"):
        return usage
    data = line[5:].strip()
    if not data or data == b"[DONE]":
        return usage
    # 绝大多数 delta 行不含 usage：先做子串判断，避免对每个 token 块都做 json.loads
    if b'"usage"' not in data:
        return usage
    try:
        obj = json.loads(data)
    except Exception:
        return usage
    u = obj.get("usage") if isinstance(obj, dict) else None
    if isinstance(u, dict):
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            v = u.get(k)
            if isinstance(v, (int, float)):
                usage[k] = int(v)
    return usage


def create_app(config: str | dict | None = None) -> FastAPI:
    # 每个 app 实例持有独立状态，避免多实例互相污染。
    client: httpx.AsyncClient | None = None
    stats = Stats()
    metrics = Metrics()
    store: UsageStore | None = None
    reqlog: RequestLog | None = None
    cost: CostTracker | None = None
    compat_store: CompatibilityStore | None = None
    budget_ledger: BudgetLedger | None = None
    writer: BatchWriter | None = None
    quota: QuotaChecker | None = None

    if isinstance(config, dict):
        base_cfg = config
        config_path: str | None = None
    else:
        config_path = _resolve_config_path(config)
        base_cfg = load_config(config_path)

    # 数据库路径优先级：环境变量 POLYGATE_DB_PATH > 配置文件 db_path > 默认值。
    # 环境变量用于容器场景：把 DB 显式指到挂载卷内，避免容器重建后数据丢失。
    db_path = (
        os.environ.get("POLYGATE_DB_PATH")
        or base_cfg.get("db_path", "polygate.db")
        or None
    )
    config_store = ConfigStore(db_path)
    client_key_store = VirtualKeyStore(db_path)
    secret_store = ProviderSecretStore(db_path)
    initial_overrides = config_store.load()

    def _import_legacy_client_keys(overrides: dict, cfg: dict | None = None) -> bool:
        """Move plaintext configuration keys into the hashed lifecycle store."""
        changed = False
        source = cfg if cfg is not None else base_cfg
        base_auth = source.get("auth") if isinstance(source.get("auth"), dict) else {}
        client_key_store.import_legacy(base_auth.get("client_keys") or [])
        ov_auth = overrides.get("auth")
        if isinstance(ov_auth, dict) and ov_auth.get("client_keys"):
            client_key_store.import_legacy(ov_auth.get("client_keys"))
            ov_auth.pop("client_keys", None)
            if not ov_auth:
                overrides.pop("auth", None)
            changed = True
        return changed

    original_overrides = copy.deepcopy(initial_overrides)
    _import_legacy_client_keys(initial_overrides)
    initial_overrides, _ = secret_store.stage_overrides(initial_overrides)
    if initial_overrides != original_overrides:
        config_store.save(initial_overrides)
    secret_store.prune(secret_store.referenced(initial_overrides))

    def _runtime_config(overrides: dict, cfg: dict | None = None) -> dict:
        merged = merge_config(base_cfg if cfg is None else cfg, overrides)
        return secret_store.resolve_merged_config(merged, overrides)

    state = RuntimeState(
        _runtime_config(initial_overrides),
        client_key_store.runtime_records(),
    )

    def _reload_runtime(cfg: dict) -> None:
        state.reload(cfg, client_key_store.runtime_records())

    def _validate_effective_config(overrides: dict) -> tuple[dict, dict]:
        cfg = _runtime_config(overrides)
        providers = cfg.get("providers") if isinstance(cfg.get("providers"), dict) else {}
        for name, provider in providers.items():
            if not isinstance(provider, dict) or not str(provider.get("base_url") or "").strip():
                raise ValueError(f"provider base_url is required: {name}")
        for model in cfg.get("models") or []:
            if not isinstance(model, dict):
                continue
            for route in model.get("providers") or []:
                if not isinstance(route, dict) or route.get("provider") not in providers:
                    provider = route.get("provider") if isinstance(route, dict) else None
                    raise ValueError(f"unknown provider in model route: {provider}")
        probe = None
        try:
            probe = RuntimeState(cfg, client_key_store.runtime_records())
            return cfg, probe.editable()
        finally:
            limiter = probe.limiter if probe is not None else None
            if limiter is not state.limiter and hasattr(limiter, "close"):
                limiter.close()

    def _settle_budget_ticket(budget_ticket, actual_cost: float) -> None:
        if quota is None or not budget_ticket:
            return
        try:
            quota.settle_budget(budget_ticket, actual_cost)
        except Exception:
            # 结算失败不能把已经成功的上游响应改写成客户端错误。
            metrics.inc("polygate_budget_settle_errors_total", {})

    def _release_budget_ticket(budget_ticket) -> None:
        if quota is None or not budget_ticket:
            return
        try:
            quota.release_budget(budget_ticket)
        except Exception:
            metrics.inc("polygate_budget_release_errors_total", {})

    def _settle_budget_usage(budget_ticket, prompt: int, completion: int, actual_cost: float) -> None:
        """有 usage 时精确结算；上游未返回 usage 时按预占上限保守结算。"""
        if not budget_ticket:
            return
        if prompt <= 0 and completion <= 0:
            actual_cost = max(actual_cost, float(budget_ticket.get("reserved", 0.0) or 0.0))
        _settle_budget_ticket(budget_ticket, actual_cost)

    # ---------- 记录与日志 ----------
    def _record_stats(
        model, provider, status, prompt=0, completion=0, latency_ms=0.0,
        key_label=None, client=None, budget_ticket=None,
    ):
        stats.record(model, provider, status, prompt, completion, latency_ms, client=client)
        if store is not None:
            store.record(model, provider, status, prompt, completion, latency_ms)
        request_cost = 0.0
        # 流式响应可能在上游已产生 token 后中断；只要拿到 usage，也要记账。
        if cost is not None and key_label and (status == "ok" or prompt > 0 or completion > 0):
            request_cost = compute_cost(prompt, completion, state.pricing, model)
            if request_cost > 0:
                cost.add(key_label, model, prompt, completion, request_cost, client=client or "")
                if quota is not None and client:
                    quota.settle_cost(client, request_cost)
        if status == "ok":
            _settle_budget_usage(budget_ticket, prompt, completion, request_cost)
        metrics.inc("polygate_requests_total", {"model": model, "provider": provider, "status": status})
        if status == "error":
            metrics.inc("polygate_errors_total", {"model": model})
        if prompt:
            metrics.inc("polygate_tokens_total", {"model": model, "type": "prompt"}, prompt)
        if completion:
            metrics.inc("polygate_tokens_total", {"model": model, "type": "completion"}, completion)
        if latency_ms:
            metrics.observe("polygate_request_duration_seconds", latency_ms / 1000.0, {"model": model})
        return request_cost

    def _log_request(ctx, provider, model_name, key_label, status, status_code, latency, request, response, error, upstream_model=None):
        if reqlog is None:
            return
        try:
            reqlog.log(
                req_id=ctx["req_id"], endpoint=ctx["endpoint"], model=model_name, provider=provider,
                key_label=key_label, status=status, status_code=status_code, latency_ms=latency,
                request=request, response=response, error=error, upstream_model=upstream_model,
                client=ctx.get("client", ""), logical_id=ctx.get("logical_id", ""),
            )
        except Exception:
            pass

    # ---------- 健康检查 ----------
    async def _ping_provider(prov) -> bool:
        """尝试所有 Key；2xx/3xx 视为健康。

        探测路径固定为 ``GET /models``：这是 OpenAI 兼容协议里最轻量的存在性检查。
        部分厂商不实现该接口（返回 404），此时只要拿到任何 HTTP 响应就说明
        「网络可达、域名有效」——真正的可用性交给业务流量与熔断器判断，
        不应因为 404 就把一个完全可用的 Provider 判死。
        """
        for key in prov.api_keys:
            try:
                resp = await client.get(prov.models_url, headers=prov.headers(key), timeout=5.0)
            except httpx.HTTPError:
                continue
            if resp.status_code < 400:
                return True
            # 404/405 说明该端点不存在，但网络与服务本身是通的 → 视为可达
            if resp.status_code in (404, 405):
                return True
        return False

    async def _health_loop():
        while True:
            await asyncio.sleep(max(5.0, state.health_interval))
            if not state.health_enabled:
                continue
            items = list(state.providers.items())
            if not items:
                continue
            # 并发探测：单个 Provider 卡住不会拖慢其它 Provider 的探测
            results = await asyncio.gather(*(_ping_provider(p) for _, p in items), return_exceptions=True)
            for (name, _), ok in zip(items, results, strict=False):
                circuit = state.circuits.get(name)
                if circuit is not None:
                    circuit.record_probe(ok is True)

    # ---------- Provider 兼容性探测 ----------
    _COMPAT_PROBES = {"models", "chat", "stream_usage", "tools", "json"}
    _COMPAT_CAPABILITY_NAMES = {
        "chat": "chat",
        "stream_usage": "stream",
        "tools": "tools",
        "json": "json",
    }

    def _message_text(data: dict) -> str:
        try:
            content = data["choices"][0]["message"].get("content")
        except (KeyError, IndexError, TypeError):
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return "".join(
                str(part.get("text", "")) for part in content if isinstance(part, dict)
            )
        return ""

    async def _compat_call(cand: Candidate, probe: str) -> dict:
        started = time.perf_counter()
        headers = cand.provider.headers(cand.api_key)
        timeout = min(60.0, max(1.0, cand.provider.timeout))
        detail: dict = {}
        status_code: int | None = None
        ok = False
        try:
            if probe == "models":
                resp = await client.get(cand.provider.models_url, headers=headers, timeout=timeout)
                status_code = resp.status_code
                ok = 200 <= resp.status_code < 400
                detail = {
                    "endpoint_present": ok,
                    "reachable": resp.status_code < 500,
                    "rate_limit_headers": _upstream_headers(resp),
                }
                if not ok:
                    detail["error"] = await resp.aread()
                    detail["error"] = bytes(detail["error"])[:500].decode("utf-8", "replace")
                return {
                    "probe": probe, "ok": ok, "status_code": status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "detail": detail,
                }

            payload: dict = {
                "model": cand.upstream_model,
                "messages": [{"role": "user", "content": "Reply with OK only."}],
                "max_tokens": 8,
                "temperature": 0,
            }
            if probe == "stream_usage":
                payload["stream"] = True
                payload["stream_options"] = {"include_usage": True}
            elif probe == "tools":
                payload["messages"][0]["content"] = "Call ping with value ok. Do not answer in prose."
                payload["tools"] = [{
                    "type": "function",
                    "function": {
                        "name": "ping",
                        "description": "Return the supplied value.",
                        "parameters": {
                            "type": "object",
                            "properties": {"value": {"type": "string"}},
                            "required": ["value"],
                        },
                    },
                }]
                payload["tool_choice"] = "auto"
                payload["max_tokens"] = 48
            elif probe == "json":
                payload["messages"][0]["content"] = 'Return only this JSON object: {"ok":true}'
                payload["response_format"] = {"type": "json_object"}
                payload["max_tokens"] = 48

            if probe == "stream_usage":
                usage: dict = {}
                buffer = b""
                async with client.stream(
                    "POST", cand.provider.chat_url, json=payload, headers=headers, timeout=timeout,
                ) as resp:
                    status_code = resp.status_code
                    detail["rate_limit_headers"] = _upstream_headers(resp)
                    if resp.status_code >= 400:
                        raw = await _bounded_error_bytes(resp)
                        detail["error"] = raw[:500].decode("utf-8", "replace")
                    else:
                        async for chunk in resp.aiter_bytes():
                            buffer += chunk
                            while b"\n" in buffer:
                                line, buffer = buffer.split(b"\n", 1)
                                if len(line) <= MAX_SSE_EVENT_BYTES:
                                    usage = _extract_stream_usage(line, usage)
                            if len(buffer) > MAX_SSE_EVENT_BYTES:
                                buffer = b""
                            if len(usage) and any(int(v or 0) > 0 for v in usage.values() if isinstance(v, (int, float))):
                                # Already enough to prove usage events are emitted.
                                break
                detail["usage"] = usage
                ok = status_code is not None and status_code < 400 and any(
                    int(v or 0) > 0 for v in usage.values() if isinstance(v, (int, float))
                )
            else:
                resp = await client.post(cand.provider.chat_url, json=payload, headers=headers, timeout=timeout)
                status_code = resp.status_code
                detail["rate_limit_headers"] = _upstream_headers(resp)
                if resp.status_code >= 400:
                    detail["error"] = resp.text[:500]
                else:
                    try:
                        data = resp.json()
                    except Exception:
                        data = {}
                        detail["error"] = "upstream returned non-JSON response"
                    if probe == "chat":
                        ok = isinstance(data, dict) and bool(data.get("choices"))
                    elif probe == "tools":
                        try:
                            message = data["choices"][0].get("message") or {}
                        except (KeyError, IndexError, TypeError):
                            message = {}
                        calls = message.get("tool_calls") or message.get("function_call")
                        ok = bool(calls)
                        detail["tool_calls"] = calls or []
                    elif probe == "json":
                        text = _message_text(data).strip()
                        if text.startswith("```"):
                            text = text.strip("`")
                            if text.startswith("json"):
                                text = text[4:].strip()
                        try:
                            parsed = json.loads(text)
                            ok = isinstance(parsed, dict)
                        except Exception:
                            ok = False
                        detail["content_preview"] = text[:500]
                    if not ok and "error" not in detail:
                        detail["error"] = f"{probe} probe did not match the expected response shape"
        except httpx.TimeoutException:
            detail = {"error": "probe timed out"}
        except httpx.HTTPError as exc:
            detail = {"error": str(exc)[:500]}
        except Exception as exc:
            detail = {"error": str(exc)[:500]}
        return {
            "probe": probe,
            "ok": ok,
            "status_code": status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            "detail": detail,
        }

    def _compat_view(rows: list[dict]) -> dict:
        routes: dict[tuple[str, str, str], dict] = {}
        for row in rows:
            key = (row["provider"], row["model"], row["upstream_model"])
            item = routes.setdefault(key, {
                "provider": row["provider"],
                "model": row["model"],
                "upstream_model": row["upstream_model"],
                "probes": {},
                "capabilities": [],
            })
            item["probes"][row["probe"]] = {
                "ok": row["ok"],
                "status_code": row["status_code"],
                "latency_ms": row["latency_ms"],
                "detail": row["detail"],
            }
        for item in routes.values():
            item["capabilities"] = sorted({
                _COMPAT_CAPABILITY_NAMES[name]
                for name, result in item["probes"].items()
                if result["ok"] and name in _COMPAT_CAPABILITY_NAMES
            })
        return {
            "run_id": rows[-1]["run_id"] if rows else None,
            "tested_at": rows[-1]["ts"] if rows else None,
            "routes": list(routes.values()),
        }

    async def _cleanup_loop():
        """按 log_retention_days 定期清理过旧的 calls / requests / costs，避免库无限增长。"""
        while True:
            await asyncio.sleep(3600)
            days = state.log_retention_days
            if not days or days <= 0 or writer is None:
                continue
            cutoff = time.time() - days * 86400
            try:
                writer.enqueue("DELETE FROM calls WHERE ts < ?", (cutoff,))
                writer.enqueue("DELETE FROM requests WHERE ts < ?", (cutoff,))
                writer.enqueue("DELETE FROM costs WHERE ts < ?", (cutoff,))
            except Exception:
                pass

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal client, store, reqlog, cost, compat_store, budget_ledger, writer, quota
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=200, max_keepalive_connections=100),
        )
        if db_path:
            writer = BatchWriter(db_path)
            store = UsageStore(db_path, writer=writer)
            reqlog = RequestLog(db_path, writer=writer)
            cost = CostTracker(db_path, writer=writer)
            budget_ledger = BudgetLedger(db_path, writer=writer)
        compat_store = CompatibilityStore(db_path, writer=writer)
        quota = QuotaChecker(limiter=state.limiter, store=reqlog, cost=cost, budget=budget_ledger)
        health_task = asyncio.create_task(_health_loop())
        cleanup_task = asyncio.create_task(_cleanup_loop()) if db_path else None
        try:
            yield
        finally:
            for task in (health_task, cleanup_task):
                if task is None:
                    continue
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
                except Exception:
                    pass
            await client.aclose()
            client = None
            if writer is not None:
                writer.close()
            if budget_ledger is not None:
                budget_ledger.close()
            for obj in (store, reqlog, cost, compat_store):
                if obj is not None:
                    obj.close()
            store = reqlog = cost = compat_store = budget_ledger = None
            writer = None
            quota = None
            config_store.close()
            client_key_store.close()
            secret_store.close()

    app = FastAPI(
        title="polygate",
        description="国产大模型统一 API 网关（OpenAI 兼容）",
        version=__version__,
        lifespan=lifespan,
    )
    app.mount("/admin/assets", StaticFiles(directory=str(WEB_DIR)), name="admin-assets")

    # ---------- 全局中间件：请求 ID + 请求体大小限制 ----------
    @app.middleware("http")
    async def _request_context(request: Request, call_next):
        """给每个请求分配 req_id（用于错误响应与请求日志对齐），并拦截超大请求体。

        请求体大小限制：默认 10MB（``POLYGATE_MAX_REQUEST_BYTES``）。
        超过限制直接返回 413，避免单个超大 prompt 打爆内存或把日志库撑爆。
        """
        req_id = uuid.uuid4().hex
        request.state.polygate_req_id = req_id

        body_limit = MAX_AUDIO_BYTES if request.url.path == "/v1/audio/transcriptions" else MAX_REQUEST_BYTES
        received = 0
        original_receive = request.receive

        async def bounded_receive():
            nonlocal received
            message = await original_receive()
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > body_limit:
                    raise RequestTooLarge()
            return message

        request._receive = bounded_receive

        content_length = request.headers.get("content-length")
        if content_length:
            try:
                if int(content_length) > body_limit:
                    return Response(
                        content=json.dumps(
                            _error_body(
                                f"request body too large (limit {body_limit} bytes)",
                                "invalid_request_error",
                                "request_too_large",
                            ),
                            ensure_ascii=False,
                        ),
                        status_code=413,
                        media_type="application/json",
                        headers={"X-Request-ID": req_id},
                    )
            except ValueError:
                pass

        try:
            response = await call_next(request)
        except RequestTooLarge:
            return Response(
                content=json.dumps(_error_body(
                    f"request body too large (limit {body_limit} bytes)",
                    "invalid_request_error", "request_too_large",
                ), ensure_ascii=False),
                status_code=413, media_type="application/json", headers={"X-Request-ID": req_id},
            )
        response.headers.setdefault("X-Request-ID", req_id)
        if request.url.path.startswith("/admin/assets/"):
            response.headers["Cache-Control"] = "no-cache"
        return response

    # ---------- 全局异常处理：统一 OpenAI 兼容错误体 + X-Request-ID ----------
    @app.exception_handler(GatewayError)
    async def _gateway_error_handler(request: Request, exc: GatewayError):
        """把 GatewayError 渲染成 OpenAI 兼容错误体，并补齐 X-Request-ID。

        这样「上游 400 就回 400」「被限流就回 429」等语义能真正到达客户端，
        而不是一律变成 502。
        """
        # 优先复用本次请求已经生成过的 req_id，保证客户端拿到的 ID 能对上请求日志
        req_id = getattr(request.state, "polygate_req_id", None) or uuid.uuid4().hex
        return _gateway_error_response(exc, req_id)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(request: Request, exc: StarletteHTTPException):
        """把 FastAPI/Starlette 的 HTTPException 也统一成 OpenAI 错误体格式。

        注意 404（未知路由）与 422（参数校验失败）也统一格式，
        OpenAI SDK 才能识别成正确的错误类型。
        """
        detail = exc.detail
        message = detail if isinstance(detail, str) else str(detail)
        headers = dict(getattr(exc, "headers", None) or {})
        req_id = getattr(request.state, "polygate_req_id", None)
        if req_id:
            headers["X-Request-ID"] = req_id
        payload = _error_body(message, default_error_type(exc.status_code))
        return Response(
            content=json.dumps(payload, ensure_ascii=False),
            status_code=exc.status_code,
            media_type="application/json",
            headers=headers,
        )

    # ---------- 鉴权与客户端治理 ----------
    def _resolve_client(request: Request, snapshot=None) -> tuple[str, object | None]:
        """Resolve a virtual key by hash, then fall back to the gateway key."""
        auth_header = request.headers.get("authorization", "")
        snapshot = snapshot or state.current
        token = auth_header[7:].strip() if auth_header.lower().startswith("bearer ") else ""
        pol = snapshot.client_keys.get(hash_client_key(token)) if token else None
        if pol is not None:
            if pol.expires_at is not None and pol.expires_at <= time.time():
                raise HTTPException(status_code=401, detail="gateway api key expired")
            client_key_store.touch(pol.id)
            return pol.name or pol.key_prefix or "client", pol
        gw = snapshot.gateway_key
        if not gw:
            return "default", None
        if hmac.compare_digest(auth_header, f"Bearer {gw}"):
            return "default", None
        raise HTTPException(status_code=401, detail="invalid gateway api key")

    def _alert(event: str, message: str) -> None:
        if state.alerter is not None:
            state.alerter.notify(event, event, message)

    async def _enforce_client(
        client_name: str,
        policy,
        model: str,
        snapshot=None,
        payload: dict | None = None,
        *,
        completion_is_relevant: bool = True,
    ) -> dict | None:
        """执行虚拟 Key 的模型白名单 / RPM / 配额 / 预算检查；超限抛 GatewayError。"""
        snapshot = snapshot or state.current
        if policy is None or quota is None:
            return None
        reason = quota.check_model(policy, model)
        if reason:
            _alert("quota_exceeded", f"客户端 {client_name}: {reason}")
            raise GatewayError(403, reason, error_type="invalid_request_error")
        limiter = snapshot.limiter
        if isinstance(limiter, RateLimiter):
            reason = quota.check_rpm(policy, client_name, limiter)
        else:
            reason = await asyncio.to_thread(quota.check_rpm, policy, client_name, limiter)
        if reason:
            _alert("quota_exceeded", f"客户端 {client_name}: {reason}")
            raise GatewayError(429, reason, error_type="rate_limit_error")
        reason = await asyncio.to_thread(quota.reserve, policy, client_name)
        if reason:
            _alert("quota_exceeded", f"客户端 {client_name}: {reason}")
            raise GatewayError(
                403 if "budget" in reason else 429, reason,
                error_type="insufficient_quota" if "budget" in reason else "rate_limit_error",
            )
        try:
            reason, ticket, _ = await asyncio.to_thread(
                quota.reserve_budget,
                policy,
                client_name,
                model,
                payload or {},
                snapshot.pricing,
                snapshot.budget,
                completion_is_relevant=completion_is_relevant,
            )
        except Exception:
            _alert("budget_ledger_error", f"客户端 {client_name}: 预算账本不可用")
            raise GatewayError(503, "budget ledger unavailable", error_type="service_unavailable") from None
        if reason:
            _alert("quota_exceeded", f"客户端 {client_name}: {reason}")
            raise GatewayError(403, reason, error_type="insufficient_quota")
        return ticket

    def _record_circuit_failure(circuit, provider_name: str) -> None:
        """记录一次 Provider 熔断失败，并在熔断首次打开时告警。"""
        was_open = circuit.snapshot()["state"] == "open"
        circuit.record_failure()
        if not was_open and circuit.snapshot()["state"] == "open":
            _alert("circuit_open", f"Provider {provider_name} 熔断已打开")

    def _check_admin(request: Request) -> None:
        key = state.admin_key
        if key and not hmac.compare_digest(request.headers.get("authorization", ""), f"Bearer {key}"):
            raise HTTPException(status_code=401, detail="invalid admin key")

    # ---------- 上游转发 ----------
    async def _forward_nonstream(cand: Candidate, payload: dict, model_name: str, url: str, ctx: dict, log_request=None) -> JSONResponse:
        # 每次上游尝试生成独立 req_id，避免多候选重试时日志 req_id 冲突
        ctx["req_id"] = uuid.uuid4().hex
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        key_label = mask_key(cand.api_key)
        client_name = ctx.get("client", "")
        budget_ticket = ctx.get("budget_ticket")
        request_for_log = log_request if log_request is not None else payload
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=_timeout_for(cand.provider))
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label, client=client_name, budget_ticket=budget_ticket)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, request_for_log, None, str(exc), upstream_model=cand.upstream_model)
            raise UpstreamError(None, str(exc)) from exc

        latency = (time.time() - start) * 1000
        if resp.status_code >= 400:
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label, client=client_name, budget_ticket=budget_ticket)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, request_for_log, None, resp.text[:500], upstream_model=cand.upstream_model)
            # 保留上游原始 body 与响应头：4xx 才能原样回传给客户端，
            # Retry-After / ratelimit-* 也才能透传出去。
            raise UpstreamError(
                resp.status_code,
                resp.text[:500],
                body=resp.content,
                headers=_upstream_headers(resp),
            )

        try:
            data = resp.json()
        except Exception as exc:
            msg = f"upstream returned non-JSON response with status {resp.status_code}: {resp.text[:200]}"
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label, client=client_name, budget_ticket=budget_ticket)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", 502, latency, request_for_log, None, msg, upstream_model=cand.upstream_model)
            raise UpstreamError(502, msg) from exc


        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens", usage.get("total_tokens", 0)) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        _record_stats(model_name, cand.provider_name, "ok", prompt=prompt, completion=completion, latency_ms=latency, key_label=key_label, client=client_name, budget_ticket=budget_ticket)
        _log_request(ctx, cand.provider_name, model_name, key_label, "ok", 200, latency, request_for_log, data, None, upstream_model=cand.upstream_model)

        headers_out = _upstream_headers(resp)
        headers_out["X-Request-ID"] = ctx["req_id"]
        headers_out["X-Polygate-Provider"] = _header_text(cand.provider_name)
        headers_out["X-Polygate-Upstream-Model"] = _header_text(cand.upstream_model)
        # 直接透传上游原始字节，避免「解析成 dict 再重新序列化」的双重开销
        return Response(content=resp.content, media_type="application/json", headers=headers_out)

    async def _stream(cand: Candidate, payload: dict, model_name: str, ctx: dict, log_request=None) -> StreamingResponse:
        # 每次上游尝试生成独立 req_id，避免多候选重试时日志 req_id 冲突
        ctx["req_id"] = uuid.uuid4().hex
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        key_label = mask_key(cand.api_key)
        client_name = ctx.get("client", "")
        budget_ticket = ctx.get("budget_ticket")
        request_for_log = log_request if log_request is not None else payload

        async def _send(payload_to_send: dict):
            req = client.build_request(
                "POST", cand.provider.chat_url, json=payload_to_send, headers=headers,
                timeout=_timeout_for(cand.provider),
            )
            return await client.send(req, stream=True)

        try:
            resp = await _send(payload)
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label, client=client_name, budget_ticket=budget_ticket)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, request_for_log, None, str(exc), upstream_model=cand.upstream_model)
            raise UpstreamError(None, str(exc)) from exc

        # 部分厂商（DashScope/GLM 等）不接受 stream_options，返回 400 时去掉后重试一次
        if resp.status_code == 400 and payload.get("stream_options"):
            await resp.aclose()
            retry_payload = {k: v for k, v in payload.items() if k != "stream_options"}
            try:
                resp = await _send(retry_payload)
            except httpx.HTTPError as exc:
                _record_stats(model_name, cand.provider_name, "error", key_label=key_label, client=client_name, budget_ticket=budget_ticket)
                _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, request_for_log, None, str(exc), upstream_model=cand.upstream_model)
                raise UpstreamError(None, str(exc)) from exc

        if resp.status_code >= 400:
            raw = await _bounded_error_bytes(resp)
            await resp.aclose()
            latency = (time.time() - start) * 1000
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label, client=client_name, budget_ticket=budget_ticket)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, request_for_log, None, raw[:500].decode("utf-8", "replace"), upstream_model=cand.upstream_model)
            raise UpstreamError(
                resp.status_code,
                raw[:500].decode("utf-8", "replace"),
                body=raw,
                headers=_upstream_headers(resp),
            )


        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        stream_error = None
        buffer = b""
        parse_error = None

        async def gen():
            nonlocal usage, stream_error, buffer, parse_error
            try:
                async for chunk in resp.aiter_bytes():
                    # Each chunk is split once; only the incomplete final line
                    # is retained, rather than repeatedly copying the stream.
                    start = 0
                    while True:
                        end = chunk.find(b"\n", start)
                        if end < 0:
                            buffer += chunk[start:]
                            break
                        line = buffer + chunk[start:end]
                        buffer = b""
                        if len(line) <= MAX_SSE_EVENT_BYTES:
                            usage = _extract_stream_usage(line, usage)
                        else:
                            parse_error = "upstream SSE event exceeded size limit"
                        start = end + 1
                    if len(buffer) > MAX_SSE_EVENT_BYTES:
                        parse_error = "upstream SSE event exceeded size limit"
                        buffer = b""
                    yield chunk
                if buffer:
                    usage = _extract_stream_usage(buffer, usage)
                if parse_error and not stream_error:
                    stream_error = parse_error
            except (asyncio.CancelledError, GeneratorExit):
                stream_error = "client disconnected"
                raise
            except Exception as exc:
                stream_error = str(exc)
                err_payload = json.dumps(
                    {"error": {"message": stream_error, "type": "upstream_stream_error"}},
                    ensure_ascii=False,
                )
                yield f"data: {err_payload}\n\n".encode()
            finally:
                await resp.aclose()
                latency = (time.time() - start) * 1000
                _record_stats(
                    model_name, cand.provider_name,
                    "error" if stream_error else "ok",
                    prompt=usage["prompt_tokens"], completion=usage["completion_tokens"],
                    latency_ms=latency, key_label=key_label, client=client_name,
                    budget_ticket=budget_ticket,
                )
                if stream_error:
                    actual = compute_cost(
                        usage["prompt_tokens"], usage["completion_tokens"], state.pricing, model_name,
                    )
                    _settle_budget_usage(
                        budget_ticket, usage["prompt_tokens"], usage["completion_tokens"], actual,
                    )
                _log_request(
                    ctx, cand.provider_name, model_name, key_label,
                    "error" if stream_error else "ok", 200 if not stream_error else None, latency,
                    request_for_log, {"stream": True, "usage": usage}, stream_error,
                    upstream_model=cand.upstream_model,
                )

        headers_out = _upstream_headers(resp)
        headers_out["X-Request-ID"] = ctx["req_id"]
        headers_out["X-Polygate-Provider"] = _header_text(cand.provider_name)
        headers_out["X-Polygate-Upstream-Model"] = _header_text(cand.upstream_model)
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers_out)

    async def _forward_transcription(cand: Candidate, model_name: str, content: bytes, filename: str, content_type: str, extra: dict, ctx: dict) -> JSONResponse:
        # 每次上游尝试生成独立 req_id，避免多候选重试时日志 req_id 冲突
        ctx["req_id"] = uuid.uuid4().hex
        start = time.time()
        key_label = mask_key(cand.api_key)
        client_name = ctx.get("client", "")
        budget_ticket = ctx.get("budget_ticket")
        headers = {}
        if cand.api_key:
            headers["Authorization"] = f"Bearer {cand.api_key}"
        payload_data = {**extra, "model": cand.upstream_model}
        files = {"file": (filename, content, content_type)}
        request_for_log = {**extra, "model": model_name}
        try:
            resp = await client.post(cand.provider.transcriptions_url, data=payload_data, files=files, headers=headers, timeout=_timeout_for(cand.provider))
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label, client=client_name, budget_ticket=budget_ticket)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, request_for_log, None, str(exc), upstream_model=cand.upstream_model)
            raise UpstreamError(None, str(exc)) from exc

        latency = (time.time() - start) * 1000
        if resp.status_code >= 400:
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label, client=client_name, budget_ticket=budget_ticket)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, request_for_log, None, resp.text[:500], upstream_model=cand.upstream_model)
            raise UpstreamError(
                resp.status_code,
                resp.text[:500],
                body=resp.content,
                headers=_upstream_headers(resp),
            )

        try:
            data = resp.json()
        except Exception as exc:
            msg = f"upstream returned non-JSON response with status {resp.status_code}: {resp.text[:200]}"
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label, client=client_name, budget_ticket=budget_ticket)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", 502, latency, request_for_log, None, msg, upstream_model=cand.upstream_model)
            raise UpstreamError(502, msg) from exc


        _record_stats(model_name, cand.provider_name, "ok", latency_ms=latency, key_label=key_label, client=client_name, budget_ticket=budget_ticket)
        _log_request(ctx, cand.provider_name, model_name, key_label, "ok", 200, latency, request_for_log, data, None, upstream_model=cand.upstream_model)
        headers_out = _upstream_headers(resp)
        headers_out["X-Request-ID"] = ctx["req_id"]
        headers_out["X-Polygate-Provider"] = _header_text(cand.provider_name)
        headers_out["X-Polygate-Upstream-Model"] = _header_text(cand.upstream_model)
        return JSONResponse(data, headers=headers_out)

    async def _try_all(cands: list[Candidate], model_name: str, fn, snapshot=None):
        """依次尝试候选（Provider × Key），并在必要时故障切换。

        错误语义（对客户端友好）：
        * 上游 4xx（401/403/404/422 等，429 除外）属于「请求本身有问题」，
          换 Key / 换 Provider 都无济于事 —— 立刻中断并 **原样回传** 状态码与响应体。
        * 上游 429 属于 Key 维度问题：换下一个 Key；全部 Key 都 429 才回 429。
        * 连接错误 / 5xx 属于 Provider 维度问题：换下一个 Provider；全挂才回 502。
        """
        snapshot = snapshot or state.current
        deadline = time.monotonic() + snapshot.total_timeout
        last: UpstreamError | None = None
        last_client_error: UpstreamError | None = None
        attempted = False
        rate_limited = False
        circuit_open = False
        for attempt, cand in enumerate(cands[: snapshot.max_attempts]):
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise GatewayError(504, f"total request timeout for model '{model_name}'", error_type="service_unavailable")
            circuit = snapshot.circuits.get(cand.provider_name)
            if circuit is not None and not circuit.allow():
                circuit_open = True
                continue
            # Key 级熔断：认证失效的 Key 短期内不再使用，避免每次请求白付一次 401 RTT
            key_breaker = snapshot.key_breakers.get(cand.provider_name, {}).get(cand.api_key)
            if key_breaker is not None and not key_breaker.allow():
                continue
            rl = snapshot.rate_limits.get(cand.provider_name)
            if rl:
                # 限流维度修正：按 Provider 计量（不是 provider:key），
                # 否则配 3 个 Key 会把有效限流放大 3 倍。
                rl_key = f"{cand.provider_name}"
                limiter = snapshot.limiter
                if isinstance(limiter, RateLimiter):
                    # 内存令牌桶是纯 CPU + 线程锁，直接调用；只有 Redis 这种阻塞 IO 才丢线程
                    ok = limiter.allow(rl_key, rl[0], rl[1])
                else:
                    ok = await asyncio.to_thread(limiter.allow, rl_key, rl[0], rl[1])
                if not ok:
                    rate_limited = True
                    continue
            attempted = True
            try:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise GatewayError(504, f"total request timeout for model '{model_name}'", error_type="service_unavailable")
                result = await asyncio.wait_for(fn(cand), timeout=remaining)
                if circuit is not None:
                    circuit.record_success()
                if key_breaker is not None:
                    key_breaker.record_success()
                return result
            except TimeoutError as exc:
                last = UpstreamError(None, "total request deadline exceeded")
                raise GatewayError(504, last.message, error_type="service_unavailable") from exc
            except UpstreamError as exc:
                last = exc
                if is_rate_limited(exc.status):
                    # 429：Key 额度维度问题 → 先换下一个 Key 重试；
                    # 同时计入 Provider 熔断失败（配额耗尽的 Provider 不该继续被大量打）。
                    if key_breaker is not None:
                        key_breaker.record_failure()
                    if circuit is not None:
                        _record_circuit_failure(circuit, cand.provider_name)
                    delay = _retry_after_seconds(exc.headers.get("retry-after"))
                    if delay is None:
                        delay = min(2.0, snapshot.retry_backoff * (2 ** min(attempt, 4)))
                    if delay > 0 and time.monotonic() + delay < deadline:
                        await asyncio.sleep(delay * random.uniform(0.5, 1.0))
                    continue
                if is_client_error(exc.status):
                    # 400/404/422：请求本身有问题 → 立刻中断，原样回传。
                    # 401/403 归入 Key 维度（见下），因为换 Key 可能有效。
                    last_client_error = exc
                    break
                if is_key_error(exc.status):
                    # 401/403：Key 维度问题 → 标记该 Key 并换下一个 Key
                    if key_breaker is not None:
                        key_breaker.record_failure()
                    if snapshot.retry_backoff and time.monotonic() + snapshot.retry_backoff < deadline:
                        await asyncio.sleep(snapshot.retry_backoff * random.uniform(0.5, 1.0))
                    continue
                if is_retryable(exc.status):
                    # 连接错误/5xx：Provider 维度问题 → 记录熔断失败并换下一个 Provider
                    if circuit is not None:
                        _record_circuit_failure(circuit, cand.provider_name)
                    continue
                # 兜底：不重试
                break

        if not attempted:
            if rate_limited and not circuit_open:
                raise GatewayError(
                    429,
                    f"rate limited for model '{model_name}'",
                    error_type="rate_limit_error",
                )
            if circuit_open:
                raise GatewayError(
                    503,
                    f"all providers circuit-open for model '{model_name}'",
                    error_type="service_unavailable",
                )

        # 客户端错误（4xx）优先原样回传：调用方需要知道是自己的问题，
        # 而不是被包装成 502 后触发无意义的 SDK 重试。
        if last_client_error is not None:
            raise GatewayError(
                last_client_error.status or 400,
                last_client_error.message,
                body=last_client_error.body,
                headers=last_client_error.headers,
                error_type=default_error_type(last_client_error.status),
            )

        # 上游持续 429：全部候选都被限流 → 明确回 429 并带上 Retry-After
        if last is not None and last.status == 429:
            raise GatewayError(
                429,
                f"all keys rate-limited for model '{model_name}'",
                body=last.body,
                headers=last.headers,
                error_type="rate_limit_error",
            )

        detail = f"all providers failed for model '{model_name}'"
        if last:
            detail += f": {last.message[:300]}"
        raise GatewayError(502, detail, error_type="api_error")


    async def _handle_chat(
        body: ChatRequest,
        client_name: str = "",
        snapshot=None,
        budget_ticket: dict | None = None,
        requirements: set[str] | None = None,
    ):
        snapshot = snapshot or state.current
        try:
            public_payload = body.model_dump(exclude_none=True)
            explicit = _normalize_requirements(public_payload.pop("polygate_requirements", []))
            required = _detected_requirements(public_payload, set(requirements or set()) | explicit)
            cands = snapshot.router.candidates(body.model, required)
        except Exception:
            _release_budget_ticket(budget_ticket)
            raise
        if not cands:
            _release_budget_ticket(budget_ticket)
            if body.model in snapshot.models and required:
                raise GatewayError(
                    400,
                    f"model '{body.model}' has no route supporting required capabilities: "
                    + ", ".join(sorted(required)),
                    error_type="invalid_request_error",
                )
            raise HTTPException(status_code=404, detail=f"unknown model: {body.model}")
        if body.stream and snapshot.stream_include_usage:
            public_payload.setdefault("stream_options", {"include_usage": True})
        logical_id = uuid.uuid4().hex
        ctx = {
            "endpoint": "/v1/chat/completions", "client": client_name,
            "logical_id": logical_id, "budget_ticket": budget_ticket,
        }

        async def fn(cand: Candidate):
            # 每次用独立的副本，绝不 mutate 原始对外 payload（否则日志/重放会拿到上游模型名）
            upstream_payload = {**public_payload, "model": cand.upstream_model}
            if body.stream:
                return await _stream(cand, upstream_payload, body.model, ctx, log_request=public_payload)
            return await _forward_nonstream(cand, upstream_payload, body.model, cand.provider.chat_url, ctx, log_request=public_payload)

        try:
            result = await _try_all(cands, body.model, fn, snapshot)
            stats.record_request(body.model, "ok", client_name)
            return result
        except (Exception, asyncio.CancelledError):
            stats.record_request(body.model, "error", client_name)
            _release_budget_ticket(budget_ticket)
            raise

    async def _handle_embeddings(
        body: EmbeddingsRequest,
        client_name: str = "",
        snapshot=None,
        budget_ticket: dict | None = None,
        requirements: set[str] | None = None,
    ):
        snapshot = snapshot or state.current
        try:
            public_payload = body.model_dump(exclude_none=True)
            explicit = _normalize_requirements(public_payload.pop("polygate_requirements", []))
            required = _detected_requirements(public_payload, set(requirements or set()) | explicit)
            cands = snapshot.router.candidates(body.model, required)
        except Exception:
            _release_budget_ticket(budget_ticket)
            raise
        if not cands:
            _release_budget_ticket(budget_ticket)
            if body.model in snapshot.models and required:
                raise GatewayError(
                    400,
                    f"embedding model '{body.model}' has no route supporting required capabilities: "
                    + ", ".join(sorted(required)),
                    error_type="invalid_request_error",
                )
            raise HTTPException(status_code=404, detail=f"unknown embedding model: {body.model}")
        logical_id = uuid.uuid4().hex
        ctx = {
            "endpoint": "/v1/embeddings", "client": client_name,
            "logical_id": logical_id, "budget_ticket": budget_ticket,
        }

        async def fn(cand: Candidate):
            upstream_payload = {**public_payload, "model": cand.upstream_model}
            return await _forward_nonstream(cand, upstream_payload, body.model, cand.provider.embeddings_url, ctx, log_request=public_payload)

        try:
            result = await _try_all(cands, body.model, fn, snapshot)
            stats.record_request(body.model, "ok", client_name)
            return result
        except (Exception, asyncio.CancelledError):
            stats.record_request(body.model, "error", client_name)
            _release_budget_ticket(budget_ticket)
            raise

    # ---------- 基础路由 ----------
    admin_headers = {
        "Cache-Control": "no-store",
        "Content-Security-Policy": (
            "default-src 'self'; script-src 'self'; style-src 'self'; "
            "img-src 'self' data:; connect-src 'self'; base-uri 'none'; "
            "form-action 'self'; frame-ancestors 'none'"
        ),
        "X-Content-Type-Options": "nosniff",
        "Referrer-Policy": "no-referrer",
    }

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(ADMIN_HTML, headers=admin_headers)

    @app.get("/admin", response_class=HTMLResponse)
    async def admin():
        return HTMLResponse(ADMIN_HTML, headers=admin_headers)

    @app.get("/healthz")
    async def healthz():
        return {
            "status": "ok",
            "version": __version__,
            "providers": len(state.providers),
            "models": len(state.models),
        }

    @app.get("/readyz")
    async def readyz():
        """就绪检查：配置已加载且至少有一个可用的 Provider Key。"""
        ready = bool(state.providers) and any(p.api_keys for p in state.providers.values())
        if not ready:
            raise HTTPException(status_code=503, detail="not ready: no providers with api keys")
        return {"status": "ready", "providers": len(state.providers), "models": len(state.models)}

    @app.get("/metrics")
    async def metrics_route():
        gauges = [("polygate_up", {}, 1.0)]
        if writer is not None:
            snapshot = writer.snapshot()
            gauges.extend((
                ("polygate_sqlite_queue_size", {}, float(snapshot["queued"])),
                ("polygate_sqlite_writes_total", {}, float(snapshot["written"])),
                ("polygate_sqlite_dropped_writes_total", {}, float(snapshot["dropped"])),
                ("polygate_sqlite_failed_writes_total", {}, float(snapshot["failed"])),
                ("polygate_sqlite_flush_timeouts_total", {}, float(snapshot["flush_timeouts"])),
            ))
        for name, c in state.circuits.items():
            s = 0 if c.state == "closed" else (1 if c.state == "open" else 2)
            gauges.append(("polygate_provider_circuit_state", {"provider": name}, s))
        return Response(
            content=metrics.render(extra_gauges=gauges),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/v1/compatibility")
    async def compatibility(
        request: Request,
        run_id: str | None = Query(None),
        limit: int = Query(1000, ge=1, le=5000),
    ):
        _check_admin(request)
        if compat_store is None:
            return {"run_id": None, "tested_at": None, "routes": []}
        rows = await asyncio.to_thread(compat_store.latest, run_id, limit)
        return _compat_view(rows)

    @app.post("/v1/compatibility/run")
    async def run_compatibility(payload: CompatibilityProbeRequest, request: Request):
        """主动运行最小成本探针；工具调用和 JSON 探针需要管理员显式选择。"""
        _check_admin(request)
        if compat_store is None:
            raise HTTPException(status_code=503, detail="compatibility store unavailable")
        probes: list[str] = []
        for probe in payload.probes or ["chat", "stream_usage"]:
            name = str(probe).strip().lower()
            if name not in _COMPAT_PROBES:
                raise HTTPException(status_code=400, detail=f"unknown compatibility probe: {name}")
            if name not in probes:
                probes.append(name)
        if not probes:
            raise HTTPException(status_code=400, detail="at least one probe is required")

        requested = {str(x).strip() for x in (payload.models or []) if str(x).strip()}
        snapshot = state.current
        routes: list[tuple[str, Candidate]] = []
        seen_routes: set[tuple[str, str, str]] = set()
        for public_model, model_routes in snapshot.models.items():
            if snapshot.model_kinds.get(public_model, "chat") != "chat":
                continue
            for route in model_routes:
                provider = snapshot.providers.get(route.provider)
                if provider is None or not provider.api_keys:
                    continue
                if requested and public_model not in requested and f"{route.provider}:{public_model}" not in requested:
                    continue
                route_key = (route.provider, public_model, route.model)
                if route_key in seen_routes:
                    continue
                seen_routes.add(route_key)
                routes.append((
                    public_model,
                    Candidate(route.provider, provider, provider.api_keys[0], route.model),
                ))
        if not routes:
            raise HTTPException(status_code=404, detail="no matching chat routes for compatibility probe")
        if len(routes) * len(probes) > 200:
            raise HTTPException(status_code=400, detail="compatibility probe limit exceeded (200 calls)")

        run_id = uuid.uuid4().hex
        for public_model, cand in routes:
            for probe in probes:
                result = await _compat_call(cand, probe)
                compat_store.record(
                    run_id=run_id,
                    provider=cand.provider_name,
                    public_model=public_model,
                    upstream_model=cand.upstream_model,
                    probe=probe,
                    ok=result["ok"],
                    status_code=result["status_code"],
                    latency_ms=result["latency_ms"],
                    detail=result["detail"],
                )
                metrics.inc(
                    "polygate_compatibility_probe_total",
                    {"provider": cand.provider_name, "probe": probe, "status": "ok" if result["ok"] else "error"},
                )
        rows = await asyncio.to_thread(compat_store.latest, run_id, 5000)
        return _compat_view(rows)

    @app.post("/v1/providers/{provider_name}/test")
    async def test_provider(
        provider_name: str,
        request: Request,
        payload: ProviderTestRequest | None = None,
    ):
        _check_admin(request)
        existing = state.providers.get(provider_name)
        base_url = str(
            payload.base_url if payload and payload.base_url else (existing.base_url if existing else "")
        ).rstrip("/")
        if not base_url:
            raise HTTPException(status_code=404, detail=f"provider not found: {provider_name}")
        api_key = payload.api_key if payload and payload.api_key else None
        if api_key is None and existing and existing.api_keys:
            api_key = existing.api_keys[0]
        headers = {"Content-Type": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        timeout = float(payload.timeout if payload else 8.0)
        started = time.perf_counter()
        status_code: int | None = None
        raw = bytearray()
        truncated = False
        try:
            async with client.stream(
                "GET",
                f"{base_url}/models",
                headers=headers,
                timeout=httpx.Timeout(timeout, connect=min(5.0, timeout)),
            ) as resp:
                status_code = resp.status_code
                async for chunk in resp.aiter_bytes():
                    remaining = 1024 * 1024 - len(raw)
                    if remaining <= 0:
                        truncated = True
                        break
                    raw.extend(chunk[:remaining])
        except httpx.HTTPError as exc:
            return {
                "provider": provider_name,
                "ok": False,
                "reachable": False,
                "status_code": None,
                "latency_ms": round((time.perf_counter() - started) * 1000, 1),
                "endpoint_present": False,
                "models": [],
                "error": str(exc)[:500],
            }

        endpoint_present = status_code not in (404, 405)
        reachable = status_code is not None and status_code < 500
        ok = bool(status_code and status_code < 400)
        discovered: list[str] = []
        error = ""
        if ok:
            try:
                body = json.loads(bytes(raw).decode("utf-8", "replace"))
                data = body.get("data") if isinstance(body, dict) else body
                if isinstance(data, list):
                    discovered = [
                        str(item.get("id"))
                        for item in data[:500]
                        if isinstance(item, dict) and item.get("id")
                    ]
            except Exception:
                error = "upstream did not return a JSON model list"
        elif raw:
            error = bytes(raw[:500]).decode("utf-8", "replace")
        result = {
            "provider": provider_name,
            "ok": ok,
            "reachable": reachable,
            "status_code": status_code,
            "latency_ms": round((time.perf_counter() - started) * 1000, 1),
            "endpoint_present": endpoint_present,
            "models": discovered,
            "error": error,
        }
        if truncated:
            result["truncated"] = True
        metrics.inc(
            "polygate_provider_test_total",
            {"provider": provider_name, "status": "ok" if ok else "error"},
        )
        return result

    @app.get("/v1/models")
    async def list_models(request: Request):
        snapshot = state.current
        _resolve_client(request, snapshot)
        # 只列出 chat 模型：embeddings / audio 模型不该出现在 OpenAI SDK 的模型列表里
        data = [
            {"id": name, "object": "model", "owned_by": routes[0].provider}
            for name, routes in snapshot.models.items()
            if snapshot.model_kinds.get(name, "chat") == "chat"
        ]
        return {"object": "list", "data": data}

    # ---------- 虚拟 Key 生命周期 ----------
    @app.get("/v1/client-keys")
    async def list_client_keys(request: Request):
        _check_admin(request)
        return [record.public() for record in client_key_store.list(include_revoked=True)]

    @app.post("/v1/client-keys")
    async def create_client_key(payload: ClientKeyCreate, request: Request):
        _check_admin(request)
        try:
            record, secret = client_key_store.create(
                name=payload.name,
                models=payload.models,
                rpm=payload.rpm,
                rpm_burst=payload.rpm_burst,
                daily_calls=payload.daily_calls,
                monthly_calls=payload.monthly_calls,
                monthly_budget=payload.monthly_budget,
                expires_at=payload.expires_at,
                note=payload.note,
            )
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        try:
            _reload_runtime(_runtime_config(config_store.load()))
        except Exception:
            client_key_store.revoke(record.id)
            raise HTTPException(status_code=500, detail="failed to publish client key") from None
        return {"key": secret, "client_key": record.public()}

    @app.patch("/v1/client-keys/{key_id}")
    async def update_client_key(key_id: str, payload: ClientKeyPatch, request: Request):
        _check_admin(request)
        try:
            record = client_key_store.update(key_id, payload.model_dump(exclude_unset=True))
        except KeyError:
            raise HTTPException(status_code=404, detail="client key not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        _reload_runtime(_runtime_config(config_store.load()))
        return record.public()

    @app.post("/v1/client-keys/{key_id}/rotate")
    async def rotate_client_key(key_id: str, request: Request):
        _check_admin(request)
        try:
            record, secret = client_key_store.rotate(key_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="client key not found") from None
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        _reload_runtime(_runtime_config(config_store.load()))
        return {"key": secret, "client_key": record.public()}

    @app.delete("/v1/client-keys/{key_id}")
    async def revoke_client_key(key_id: str, request: Request):
        _check_admin(request)
        try:
            record = client_key_store.revoke(key_id)
        except KeyError:
            raise HTTPException(status_code=404, detail="client key not found") from None
        _reload_runtime(_runtime_config(config_store.load()))
        return record.public()

    # ---------- 配置读写（存 SQLite，绝不写回 yaml；热重载） ----------
    @app.get("/v1/config")
    async def get_config(request: Request):
        _check_admin(request)
        revision = config_store.revision()
        view = state.editable()
        view["config_path"] = config_path or "(内存配置)"
        view["storage"] = "sqlite" if db_path else "memory"
        view["yaml_writable"] = False
        view["revision"] = revision
        return JSONResponse(
            view,
            headers={"ETag": f'"{revision}"', "Cache-Control": "no-store"},
        )

    @app.post("/v1/config/validate")
    async def validate_config(payload: ConfigPatch, request: Request):
        _check_admin(request)
        current_revision = config_store.revision()
        if payload.revision and payload.revision != current_revision:
            raise HTTPException(status_code=409, detail="configuration changed; reload before saving")
        overrides, changed = _merge_config_patch(config_store.load(), payload)
        try:
            _, candidate = _validate_effective_config(overrides)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid configuration: {exc}") from None
        return {
            "ok": True,
            "revision": current_revision,
            "changed": changed,
            "diff": _config_diff(state.editable(), candidate),
        }

    @app.put("/v1/config")
    async def put_config(payload: ConfigPatch, request: Request):
        _check_admin(request)
        current_overrides = config_store.load()
        current_revision = config_store.revision()
        if payload.revision and payload.revision != current_revision:
            raise HTTPException(status_code=409, detail="configuration changed; reload before saving")
        overrides, changed = _merge_config_patch(current_overrides, payload)
        try:
            candidate_cfg, candidate_view = _validate_effective_config(overrides)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=f"invalid configuration: {exc}") from None
        diff = _config_diff(state.editable(), candidate_view)
        persisted_overrides, created_refs = secret_store.stage_overrides(overrides)
        if payload.revision:
            saved = config_store.save_if_revision(persisted_overrides, payload.revision)
        else:
            config_store.save(persisted_overrides)
            saved = True
        if not saved:
            secret_store.delete(created_refs)
            raise HTTPException(status_code=409, detail="configuration changed; reload before saving")
        try:
            published_cfg = secret_store.resolve_merged_config(
                merge_config(base_cfg, persisted_overrides), persisted_overrides
            )
            _reload_runtime(published_cfg)
        except Exception:
            config_store.save(current_overrides)
            secret_store.delete(created_refs)
            raise HTTPException(status_code=500, detail="configuration publish failed; previous snapshot retained") from None
        secret_store.prune(secret_store.referenced(persisted_overrides))
        revision = config_store.revision()
        return {
            "ok": True, "changed": changed,
            "providers": list(state.providers.keys()), "budget": dict(state.budget),
            "revision": revision, "diff": diff,
        }

    @app.delete("/v1/config")
    async def reset_config(request: Request):
        """清除所有 SQLite 覆盖，回到 config.yaml 的原始配置。"""
        _check_admin(request)
        try:
            _reload_runtime(_runtime_config({}))
        except Exception:
            raise HTTPException(status_code=500, detail="failed to build base configuration") from None
        config_store.save({})
        secret_store.prune(set())
        return {
            "ok": True,
            "providers": list(state.providers.keys()),
            "revision": config_store.revision(),
        }

    @app.post("/v1/config/reload")
    async def reload_config(request: Request):
        nonlocal base_cfg
        _check_admin(request)
        new_cfg = load_config(config_path) if config_path else base_cfg
        overrides = config_store.load()
        original = copy.deepcopy(overrides)
        _import_legacy_client_keys(overrides, new_cfg)
        overrides, created_refs = secret_store.stage_overrides(overrides)
        published_cfg = secret_store.resolve_merged_config(merge_config(new_cfg, overrides), overrides)
        try:
            _reload_runtime(published_cfg)
        except Exception:
            secret_store.delete(created_refs)
            raise HTTPException(status_code=400, detail="invalid configuration: runtime build failed") from None
        base_cfg = new_cfg
        if overrides != original:
            config_store.save(overrides)
        secret_store.prune(secret_store.referenced(overrides))
        return {
            "ok": True,
            "providers": list(state.providers.keys()),
            "revision": config_store.revision(),
        }

    # ---------- 状态与统计 ----------
    @app.get("/v1/status")
    async def status(request: Request):
        _check_admin(request)
        prov_status = {}
        for name, c in state.circuits.items():
            prov_status[name] = {
                **c.snapshot(),
                "keys": len(state.providers[name].api_keys),
                "rate_limit": state.rate_limits.get(name),
            }
        clients = {}
        for pol in state.client_keys.values():
            name = pol.name or pol.key_prefix or "client"
            u = await asyncio.to_thread(quota.usage, pol, name) if quota is not None else {}
            clients[name] = {
                "id": pol.id,
                "key_prefix": pol.key_prefix,
                "models": sorted(pol.models),
                "rpm": pol.rpm,
                "daily_calls": pol.daily_calls,
                "monthly_calls": pol.monthly_calls,
                "monthly_budget": pol.monthly_budget,
                "expires_at": pol.expires_at,
                "usage": u,
            }
        alerting = state.alerter.snapshot() if state.alerter is not None else {"enabled": False, "muted_events": 0, "cooldown_sec": 0}
        quota_persistent = bool(db_path)
        return {
            "version": __version__,
            "providers": prov_status,
            "models": [
                {
                    "id": name,
                    "kind": state.model_kinds.get(name, "chat"),
                    "routes": [
                        {
                            "provider": r.provider, "model": r.model,
                            "weight": r.weight, "capabilities": sorted(r.capabilities),
                        }
                        for r in routes
                    ],
                }
                for name, routes in state.models.items()
            ],
            "clients": clients,
            "alerting": alerting,
            "quota_persistent": quota_persistent,
            "quota_note": "" if quota_persistent else "未配置 db_path，仅 RPM/白名单生效",
        }

    @app.get("/v1/usage")
    async def usage(request: Request):
        _check_admin(request)
        snap = stats.snapshot()
        snap["scope"] = "process"
        if store is not None:
            # DB 读取放到线程里，避免大表 COUNT 阻塞事件循环
            snap["persisted_total_calls"] = await asyncio.to_thread(store.total_calls)
        else:
            snap["persisted_total_calls"] = 0
        return snap

    @app.get("/v1/usage/history")
    async def usage_history(request: Request, days: int = Query(7, ge=1, le=90)):
        _check_admin(request)
        if store is None:
            return []
        return await asyncio.to_thread(store.daily_summary, days)

    @app.get("/v1/costs")
    async def costs(request: Request):
        _check_admin(request)
        if cost is None:
            raise HTTPException(status_code=503, detail="cost tracking disabled (no db_path)")
        return await asyncio.to_thread(cost.summary)

    @app.get("/v1/requests")
    async def list_requests(
        request: Request,
        limit: int = Query(50, ge=1, le=500),
        offset: int = Query(0, ge=0),
        model: str = Query(None),
        status: str = Query(None),
        client: str = Query(None),
    ):
        _check_admin(request)
        if reqlog is None:
            return []
        return await asyncio.to_thread(reqlog.list, limit, offset, model, status, client)

    @app.get("/v1/requests/groups")
    async def list_request_groups(
        request: Request,
        limit: int = Query(50, ge=1, le=200),
        offset: int = Query(0, ge=0),
        model: str = Query(None),
        status: str = Query(None),
        client: str = Query(None),
    ):
        _check_admin(request)
        if reqlog is None:
            return []
        return await asyncio.to_thread(reqlog.groups, limit, offset, model, status, client)

    @app.get("/v1/requests/groups/{group_id}")
    async def get_request_group(group_id: str, request: Request):
        _check_admin(request)
        if reqlog is None:
            raise HTTPException(status_code=503, detail="request logging disabled")
        entry = await asyncio.to_thread(reqlog.group, group_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"request group not found: {group_id}")
        return entry

    @app.get("/v1/requests/{req_id}")
    async def get_request(req_id: str, request: Request):
        _check_admin(request)
        if reqlog is None:
            raise HTTPException(status_code=503, detail="request logging disabled")
        entry = await asyncio.to_thread(reqlog.get, req_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"request not found: {req_id}")
        return entry

    @app.post("/v1/requests/{req_id}/replay")
    async def replay(req_id: str, request: Request):
        _check_admin(request)
        if reqlog is None:
            raise HTTPException(status_code=503, detail="request logging disabled")
        entry = await asyncio.to_thread(reqlog.get, req_id)
        if not entry or not entry.get("request"):
            raise HTTPException(status_code=404, detail=f"request not found: {req_id}")
        endpoint = entry["endpoint"]
        req = entry["request"]
        if endpoint == "/v1/chat/completions":
            return await _handle_chat(ChatRequest(**req))
        if endpoint == "/v1/embeddings":
            return await _handle_embeddings(EmbeddingsRequest(**req))
        raise HTTPException(status_code=400, detail=f"cannot replay endpoint: {endpoint}")

    @app.post("/v1/replay-lab")
    async def replay_lab(payload: ReplayLabRequest, request: Request):
        """把同一条历史请求依次重放到多个公开模型，并生成对比结果。"""
        _check_admin(request)
        if reqlog is None:
            raise HTTPException(status_code=503, detail="request logging disabled")
        entry = await asyncio.to_thread(reqlog.get, payload.request_id)
        if not entry or not entry.get("request"):
            raise HTTPException(status_code=404, detail=f"request not found: {payload.request_id}")

        endpoint = entry["endpoint"]
        if endpoint not in ("/v1/chat/completions", "/v1/embeddings"):
            raise HTTPException(status_code=400, detail=f"cannot replay endpoint: {endpoint}")
        base_request = copy.deepcopy(entry["request"])
        original_model = str(base_request.get("model") or "")
        targets: list[str] = []
        for name in (payload.models or [original_model]):
            name = str(name).strip()
            if name and name not in targets:
                targets.append(name)
        if not targets:
            raise HTTPException(status_code=400, detail="no replay target models")

        target_snapshot = state.current
        expected_kind = "embeddings" if endpoint == "/v1/embeddings" else "chat"
        results: list[dict] = []
        for target_model in targets:
            if target_model not in target_snapshot.models:
                results.append({
                    "model": target_model,
                    "ok": False,
                    "status_code": 404,
                    "latency_ms": 0,
                    "error": f"unknown model: {target_model}",
                })
                continue
            actual_kind = target_snapshot.model_kinds.get(target_model, "chat")
            if actual_kind != expected_kind:
                results.append({
                    "model": target_model,
                    "ok": False,
                    "status_code": 400,
                    "latency_ms": 0,
                    "error": f"model kind is '{actual_kind}', expected '{expected_kind}'",
                })
                continue
            replay_request = copy.deepcopy(base_request)
            replay_request["model"] = target_model
            # 对比必须拿到完整 JSON；原始请求即使开启 stream，也只在这里关闭。
            replay_request["stream"] = False
            replay_request.pop("stream_options", None)
            started = time.perf_counter()
            try:
                if endpoint == "/v1/chat/completions":
                    response = await _handle_chat(
                        ChatRequest(**replay_request), client_name="replay-lab", snapshot=target_snapshot,
                    )
                else:
                    response = await _handle_embeddings(
                        EmbeddingsRequest(**replay_request), client_name="replay-lab", snapshot=target_snapshot,
                    )
                latency_ms = (time.perf_counter() - started) * 1000
                data, prompt, completion = _response_payload(response, payload.include_response)
                usage_available = prompt > 0 or completion > 0
                results.append({
                    "model": target_model,
                    "ok": True,
                    "status_code": int(getattr(response, "status_code", 200)),
                    "provider": response.headers.get("x-polygate-provider", ""),
                    "upstream_model": response.headers.get("x-polygate-upstream-model", ""),
                    "latency_ms": round(latency_ms, 3),
                    "cost": (
                        round(compute_cost(prompt, completion, target_snapshot.pricing, target_model), 8)
                        if usage_available else None
                    ),
                    "usage": {"prompt_tokens": prompt, "completion_tokens": completion},
                    "usage_available": usage_available,
                    "response": data,
                })
            except GatewayError as exc:
                results.append({
                    "model": target_model,
                    "ok": False,
                    "status_code": exc.status_code,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error": exc.message,
                })
            except HTTPException as exc:
                results.append({
                    "model": target_model,
                    "ok": False,
                    "status_code": int(exc.status_code),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error": str(exc.detail),
                })
            except Exception as exc:
                results.append({
                    "model": target_model,
                    "ok": False,
                    "status_code": 500,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "error": str(exc)[:500],
                })

        successful = [r for r in results if r.get("ok")]
        fastest = min(successful, key=lambda r: r["latency_ms"])["model"] if successful else None
        priced = [r for r in successful if r.get("cost") is not None]
        cheapest = min(priced, key=lambda r: r["cost"])["model"] if priced else None
        return {
            "source_request_id": payload.request_id,
            "source_model": original_model,
            "endpoint": endpoint,
            "results": results,
            "summary": {
                "all_ok": len(successful) == len(results),
                "successful": len(successful),
                "failed": len(results) - len(successful),
                "fastest_model": fastest,
                "lowest_cost_model": cheapest,
            },
        }

    # ---------- 推理接口 ----------
    @app.post("/v1/chat/completions")
    async def chat_completions(body: ChatRequest, request: Request):
        snapshot = state.current
        client_name, policy = _resolve_client(request, snapshot)
        requirements = _normalize_requirements(request.headers.get("x-polygate-require", ""))
        budget_ticket = await _enforce_client(
            client_name, policy, body.model, snapshot, body.model_dump(exclude_none=True),
        )
        return await _handle_chat(body, client_name, snapshot, budget_ticket, requirements)

    @app.post("/v1/embeddings")
    async def embeddings(body: EmbeddingsRequest, request: Request):
        snapshot = state.current
        client_name, policy = _resolve_client(request, snapshot)
        requirements = _normalize_requirements(request.headers.get("x-polygate-require", ""))
        budget_ticket = await _enforce_client(
            client_name, policy, body.model, snapshot, body.model_dump(exclude_none=True),
            completion_is_relevant=False,
        )
        return await _handle_embeddings(body, client_name, snapshot, budget_ticket, requirements)

    @app.post("/v1/completions")
    async def completions(body: dict, request: Request):
        snapshot = state.current
        client_name, policy = _resolve_client(request, snapshot)
        prompt = body.get("prompt")
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        elif isinstance(prompt, list):
            messages = [{"role": "user", "content": "".join(str(p) for p in prompt)}]
        else:
            raise HTTPException(status_code=400, detail="prompt must be a string or list")
        chat_body = {k: v for k, v in body.items() if k not in ("prompt",)}
        chat_body["messages"] = messages
        chat_req = ChatRequest(**chat_body)
        requirements = _normalize_requirements(request.headers.get("x-polygate-require", ""))
        budget_ticket = await _enforce_client(
            client_name, policy, chat_req.model, snapshot, chat_req.model_dump(exclude_none=True),
        )
        return await _handle_chat(chat_req, client_name, snapshot, budget_ticket, requirements)

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        snapshot = state.current
        client_name, policy = _resolve_client(request, snapshot)
        form = await request.form()
        model = str(form.get("model") or "")
        file = form.get("file")
        if not model or file is None:
            raise HTTPException(status_code=400, detail="model and file are required")
        if not hasattr(file, "read"):
            raise HTTPException(status_code=400, detail="file must be an uploaded file")

        content = await file.read()
        filename = getattr(file, "filename", None) or "audio.bin"
        content_type = getattr(file, "content_type", None) or "application/octet-stream"
        extra = {k: v for k, v in form.items() if k not in ("model", "file")}
        budget_ticket = await _enforce_client(
            client_name, policy, model, snapshot,
            {"model": model, **extra, "file_bytes": len(content)},
            completion_is_relevant=False,
        )

        try:
            requirements = _normalize_requirements(request.headers.get("x-polygate-require", ""))
            cands = snapshot.router.candidates(model, requirements)
        except Exception:
            _release_budget_ticket(budget_ticket)
            raise
        if not cands:
            _release_budget_ticket(budget_ticket)
            if model in snapshot.models and requirements:
                raise GatewayError(
                    400,
                    f"audio model '{model}' has no route supporting required capabilities: "
                    + ", ".join(sorted(requirements)),
                    error_type="invalid_request_error",
                )
            raise HTTPException(status_code=404, detail=f"unknown model: {model}")

        logical_id = uuid.uuid4().hex
        ctx = {
            "endpoint": "/v1/audio/transcriptions", "client": client_name,
            "logical_id": logical_id, "budget_ticket": budget_ticket,
        }

        async def fn(cand: Candidate):
            return await _forward_transcription(cand, model, content, filename, content_type, extra, ctx)

        try:
            result = await _try_all(cands, model, fn, snapshot)
            stats.record_request(model, "ok", client_name)
            return result
        except (Exception, asyncio.CancelledError):
            stats.record_request(model, "error", client_name)
            _release_budget_ticket(budget_ticket)
            raise

    return app
