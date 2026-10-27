"""FastAPI 网关服务。

统一 /v1/chat/completions、/v1/embeddings、/v1/audio/transcriptions，
支持流式透传、多 Key 轮询、权重负载均衡、故障切换、令牌桶限流（内存/Redis）、
熔断器、SQLite 持久化、请求日志与重放、按 Key 成本记账、Prometheus 指标与管理面板，
并支持在管理面板中在线修改配置（base_url / api_key）后热重载，无需重启。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import __version__
from .admin import ADMIN_HTML
from .config import load_config
from .configstore import ConfigStore, merge_config
from .cost import CostTracker, compute_cost
from .db import BatchWriter
from .errors import UpstreamError, is_key_error, is_retryable
from .metrics import Metrics
from .requestlog import RequestLog
from .router import Candidate
from .runtime import RuntimeState, mask_key
from .schemas import ChatRequest, EmbeddingsRequest
from .stats import Stats
from .store import UsageStore

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


def _upstream_headers(resp: httpx.Response) -> dict:
    """透传上游的排障/限流响应头，并把上游 x-request-id 改名避免与网关冲突。"""
    out: dict = {}
    if resp.headers.get("x-request-id"):
        out["X-Upstream-Request-Id"] = resp.headers["x-request-id"]
    for name in _PASSTHROUGH_HEADERS:
        if name in resp.headers:
            out[name] = resp.headers[name]
    return out


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


def create_app(config: Optional[str | dict] = None) -> FastAPI:
    # 每个 app 实例持有独立状态，避免多实例互相污染。
    client: Optional[httpx.AsyncClient] = None
    stats = Stats()
    metrics = Metrics()
    store: Optional[UsageStore] = None
    reqlog: Optional[RequestLog] = None
    cost: Optional[CostTracker] = None
    writer: Optional[BatchWriter] = None

    if isinstance(config, dict):
        base_cfg = config
        config_path: Optional[str] = None
    else:
        config_path = _resolve_config_path(config)
        base_cfg = load_config(config_path)

    db_path = base_cfg.get("db_path", "polygate.db") or None
    config_store = ConfigStore(db_path)
    state = RuntimeState(merge_config(base_cfg, config_store.load()))

    # ---------- 记录与日志 ----------
    def _record_stats(model, provider, status, prompt=0, completion=0, latency_ms=0.0, key_label=None):
        stats.record(model, provider, status, prompt, completion, latency_ms)
        if store is not None:
            store.record(model, provider, status, prompt, completion, latency_ms)
        if cost is not None and status == "ok" and key_label:
            c = compute_cost(prompt, completion, state.pricing, model)
            if c > 0:
                cost.add(key_label, model, prompt, completion, c)
        metrics.inc("polygate_requests_total", {"model": model, "provider": provider, "status": status})
        if status == "error":
            metrics.inc("polygate_errors_total", {"model": model})
        if prompt:
            metrics.inc("polygate_tokens_total", {"model": model, "type": "prompt"}, prompt)
        if completion:
            metrics.inc("polygate_tokens_total", {"model": model, "type": "completion"}, completion)
        if latency_ms:
            metrics.observe("polygate_request_duration_seconds", latency_ms / 1000.0, {"model": model})

    def _log_request(ctx, provider, model_name, key_label, status, status_code, latency, request, response, error, upstream_model=None):
        if reqlog is None:
            return
        try:
            reqlog.log(
                req_id=ctx["req_id"], endpoint=ctx["endpoint"], model=model_name, provider=provider,
                key_label=key_label, status=status, status_code=status_code, latency_ms=latency,
                request=request, response=response, error=error, upstream_model=upstream_model,
            )
        except Exception:
            pass

    # ---------- 健康检查 ----------
    async def _ping_provider(prov) -> bool:
        """尝试所有 Key；2xx/3xx 视为健康，401/403/404/5xx 视为该 Key 不健康。"""
        for key in prov.api_keys:
            try:
                resp = await client.get(prov.models_url, headers=prov.headers(key), timeout=5.0)
                if resp.status_code < 400:
                    return True
            except httpx.HTTPError:
                continue
        return False

    async def _health_loop():
        while True:
            await asyncio.sleep(max(5.0, state.health_interval))
            if not state.health_enabled:
                continue
            for name, prov in list(state.providers.items()):
                ok = await _ping_provider(prov)
                circuit = state.circuits.get(name)
                if circuit is not None:
                    circuit.record_probe(ok)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        nonlocal client, store, reqlog, cost, writer
        client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=200),
        )
        if db_path:
            writer = BatchWriter(db_path)
            store = UsageStore(db_path, writer=writer)
            reqlog = RequestLog(db_path, writer=writer)
            cost = CostTracker(db_path, writer=writer)
        health_task = asyncio.create_task(_health_loop())
        try:
            yield
        finally:
            health_task.cancel()
            await client.aclose()
            client = None
            if writer is not None:
                writer.close()
            for obj in (store, reqlog, cost):
                if obj is not None:
                    obj.close()
            store = reqlog = cost = None
            writer = None
            config_store.close()

    app = FastAPI(
        title="polygate",
        description="国产大模型统一 API 网关（OpenAI 兼容）",
        version=__version__,
        lifespan=lifespan,
    )

    # ---------- 鉴权 ----------
    def _check_auth(request: Request) -> None:
        key = state.gateway_key
        if not key:
            return
        if request.headers.get("authorization", "") != f"Bearer {key}":
            raise HTTPException(status_code=401, detail="invalid gateway api key")

    def _check_admin(request: Request) -> None:
        key = state.admin_key
        if key and request.headers.get("authorization", "") != f"Bearer {key}":
            raise HTTPException(status_code=401, detail="invalid admin key")

    # ---------- 上游转发 ----------
    async def _forward_nonstream(cand: Candidate, payload: dict, model_name: str, url: str, ctx: dict, log_request=None) -> JSONResponse:
        # 每次上游尝试生成独立 req_id，避免多候选重试时日志 req_id 冲突
        ctx["req_id"] = uuid.uuid4().hex
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        key_label = mask_key(cand.api_key)
        request_for_log = log_request if log_request is not None else payload
        try:
            resp = await client.post(url, json=payload, headers=headers, timeout=_timeout_for(cand.provider))
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, request_for_log, None, str(exc), upstream_model=cand.upstream_model)
            raise UpstreamError(None, str(exc))

        latency = (time.time() - start) * 1000
        if resp.status_code >= 400:
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, request_for_log, None, resp.text[:500], upstream_model=cand.upstream_model)
            raise UpstreamError(resp.status_code, resp.text[:500])

        try:
            data = resp.json()
        except Exception:
            msg = f"upstream returned non-JSON response with status {resp.status_code}: {resp.text[:200]}"
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", 502, latency, request_for_log, None, msg, upstream_model=cand.upstream_model)
            raise UpstreamError(502, msg)

        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        _record_stats(model_name, cand.provider_name, "ok", prompt=prompt, completion=completion, latency_ms=latency, key_label=key_label)
        _log_request(ctx, cand.provider_name, model_name, key_label, "ok", 200, latency, request_for_log, data, None, upstream_model=cand.upstream_model)

        headers_out = _upstream_headers(resp)
        headers_out["X-Request-ID"] = ctx["req_id"]
        return JSONResponse(data, headers=headers_out)

    async def _stream(cand: Candidate, payload: dict, model_name: str, ctx: dict, log_request=None) -> StreamingResponse:
        # 每次上游尝试生成独立 req_id，避免多候选重试时日志 req_id 冲突
        ctx["req_id"] = uuid.uuid4().hex
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        key_label = mask_key(cand.api_key)
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
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, request_for_log, None, str(exc), upstream_model=cand.upstream_model)
            raise UpstreamError(None, str(exc))

        # 部分厂商（DashScope/GLM 等）不接受 stream_options，返回 400 时去掉后重试一次
        if resp.status_code == 400 and payload.get("stream_options"):
            await resp.aclose()
            retry_payload = {k: v for k, v in payload.items() if k != "stream_options"}
            try:
                resp = await _send(retry_payload)
            except httpx.HTTPError as exc:
                _record_stats(model_name, cand.provider_name, "error", key_label=key_label)
                _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, request_for_log, None, str(exc), upstream_model=cand.upstream_model)
                raise UpstreamError(None, str(exc))

        if resp.status_code >= 400:
            raw = await resp.aread()
            await resp.aclose()
            latency = (time.time() - start) * 1000
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, request_for_log, None, raw[:500].decode("utf-8", "replace"), upstream_model=cand.upstream_model)
            raise UpstreamError(resp.status_code, raw[:500].decode("utf-8", "replace"))

        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        stream_error = None
        buffer = b""

        async def gen():
            nonlocal usage, stream_error, buffer
            try:
                async for chunk in resp.aiter_bytes():
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        usage = _extract_stream_usage(line, usage)
                    yield chunk
                if buffer:
                    usage = _extract_stream_usage(buffer, usage)
            except Exception as exc:
                stream_error = str(exc)
                err_payload = json.dumps(
                    {"error": {"message": stream_error, "type": "upstream_stream_error"}},
                    ensure_ascii=False,
                )
                yield f"data: {err_payload}\n\n".encode("utf-8")
            finally:
                await resp.aclose()
                latency = (time.time() - start) * 1000
                _record_stats(
                    model_name, cand.provider_name,
                    "error" if stream_error else "ok",
                    prompt=usage["prompt_tokens"], completion=usage["completion_tokens"],
                    latency_ms=latency, key_label=key_label,
                )
                _log_request(
                    ctx, cand.provider_name, model_name, key_label,
                    "error" if stream_error else "ok", 200 if not stream_error else None, latency,
                    request_for_log, {"stream": True, "usage": usage}, stream_error,
                    upstream_model=cand.upstream_model,
                )

        headers_out = _upstream_headers(resp)
        headers_out["X-Request-ID"] = ctx["req_id"]
        return StreamingResponse(gen(), media_type="text/event-stream", headers=headers_out)

    async def _forward_transcription(cand: Candidate, model_name: str, content: bytes, filename: str, content_type: str, extra: dict, ctx: dict) -> JSONResponse:
        # 每次上游尝试生成独立 req_id，避免多候选重试时日志 req_id 冲突
        ctx["req_id"] = uuid.uuid4().hex
        start = time.time()
        key_label = mask_key(cand.api_key)
        headers = {}
        if cand.api_key:
            headers["Authorization"] = f"Bearer {cand.api_key}"
        payload_data = {**extra, "model": cand.upstream_model}
        files = {"file": (filename, content, content_type)}
        request_for_log = {**extra, "model": model_name}
        try:
            resp = await client.post(cand.provider.transcriptions_url, data=payload_data, files=files, headers=headers, timeout=_timeout_for(cand.provider))
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, request_for_log, None, str(exc), upstream_model=cand.upstream_model)
            raise UpstreamError(None, str(exc))

        latency = (time.time() - start) * 1000
        if resp.status_code >= 400:
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, request_for_log, None, resp.text[:500], upstream_model=cand.upstream_model)
            raise UpstreamError(resp.status_code, resp.text[:500])

        try:
            data = resp.json()
        except Exception:
            msg = f"upstream returned non-JSON response with status {resp.status_code}: {resp.text[:200]}"
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", 502, latency, request_for_log, None, msg, upstream_model=cand.upstream_model)
            raise UpstreamError(502, msg)

        _record_stats(model_name, cand.provider_name, "ok", latency_ms=latency, key_label=key_label)
        _log_request(ctx, cand.provider_name, model_name, key_label, "ok", 200, latency, request_for_log, data, None, upstream_model=cand.upstream_model)
        headers_out = _upstream_headers(resp)
        headers_out["X-Request-ID"] = ctx["req_id"]
        return JSONResponse(data, headers=headers_out)

    async def _try_all(cands: list[Candidate], model_name: str, fn):
        last: Optional[UpstreamError] = None
        attempted = False
        rate_limited = False
        circuit_open = False
        for cand in cands:
            circuit = state.circuits.get(cand.provider_name)
            if circuit is not None and not circuit.allow():
                circuit_open = True
                continue
            rl = state.rate_limits.get(cand.provider_name)
            if rl:
                ok = await asyncio.to_thread(state.limiter.allow, f"{cand.provider_name}:{cand.api_key}", rl[0], rl[1])
                if not ok:
                    rate_limited = True
                    continue
            attempted = True
            try:
                result = await fn(cand)
                if circuit is not None:
                    circuit.record_success()
                return result
            except UpstreamError as exc:
                last = exc
                if is_key_error(exc.status):
                    # 401/403/429：Key 维度问题 → 换下一个 Key
                    continue
                if is_retryable(exc.status):
                    # 连接错误/5xx：Provider 维度问题 → 换下一个 Provider
                    if circuit is not None:
                        circuit.record_failure()
                    continue
                # 400/404/422：请求本身有问题，换 Key/Provider 都无济于事
                break

        if not attempted:
            if rate_limited and not circuit_open:
                raise HTTPException(status_code=429, detail=f"rate limited for model '{model_name}'")
            if circuit_open:
                raise HTTPException(status_code=503, detail=f"all providers circuit-open for model '{model_name}'")
        detail = f"all providers failed for model '{model_name}'"
        if last:
            detail += f": {last.message[:300]}"
        raise HTTPException(status_code=502, detail=detail)

    async def _handle_chat(body: ChatRequest):
        cands = state.router.candidates(body.model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown model: {body.model}")
        public_payload = body.model_dump(exclude_none=True)
        if body.stream and state.stream_include_usage:
            public_payload.setdefault("stream_options", {"include_usage": True})
        ctx = {"endpoint": "/v1/chat/completions"}

        async def fn(cand: Candidate):
            # 每次用独立的副本，绝不 mutate 原始对外 payload（否则日志/重放会拿到上游模型名）
            upstream_payload = {**public_payload, "model": cand.upstream_model}
            if body.stream:
                return await _stream(cand, upstream_payload, body.model, ctx, log_request=public_payload)
            return await _forward_nonstream(cand, upstream_payload, body.model, cand.provider.chat_url, ctx, log_request=public_payload)

        return await _try_all(cands, body.model, fn)

    async def _handle_embeddings(body: EmbeddingsRequest):
        cands = state.router.candidates(body.model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown embedding model: {body.model}")
        public_payload = body.model_dump(exclude_none=True)
        ctx = {"endpoint": "/v1/embeddings"}

        async def fn(cand: Candidate):
            upstream_payload = {**public_payload, "model": cand.upstream_model}
            return await _forward_nonstream(cand, upstream_payload, body.model, cand.provider.embeddings_url, ctx, log_request=public_payload)

        return await _try_all(cands, body.model, fn)

    # ---------- 基础路由 ----------
    @app.get("/", response_class=HTMLResponse)
    async def index():
        return HTMLResponse(ADMIN_HTML)

    @app.get("/admin", response_class=HTMLResponse)
    async def admin():
        return HTMLResponse(ADMIN_HTML)

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
        for name, c in state.circuits.items():
            s = 0 if c.state == "closed" else (1 if c.state == "open" else 2)
            gauges.append(("polygate_provider_circuit_state", {"provider": name}, s))
        return Response(
            content=metrics.render(extra_gauges=gauges),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/v1/models")
    async def list_models(request: Request):
        _check_auth(request)
        # 只列出 chat 模型：embeddings / audio 模型不该出现在 OpenAI SDK 的模型列表里
        data = [
            {"id": name, "object": "model", "owned_by": routes[0].provider}
            for name, routes in state.models.items()
            if state.model_kinds.get(name, "chat") == "chat"
        ]
        return {"object": "list", "data": data}

    # ---------- 配置读写（存 SQLite，绝不写回 yaml；热重载） ----------
    @app.get("/v1/config")
    async def get_config(request: Request):
        _check_admin(request)
        view = state.editable()
        view["config_path"] = config_path or "(内存配置)"
        view["storage"] = "sqlite" if db_path else "memory"
        view["yaml_writable"] = False
        return view

    @app.put("/v1/config")
    async def put_config(payload: dict, request: Request):
        _check_admin(request)
        updates = payload.get("providers") or {}
        if not isinstance(updates, dict):
            raise HTTPException(status_code=400, detail="providers must be an object")

        overrides = config_store.load()
        ov_providers = overrides.setdefault("providers", {})
        changed = []
        for name, patch in updates.items():
            if not isinstance(patch, dict):
                continue
            prov = ov_providers.setdefault(name, {})
            if patch.get("base_url"):
                prov["base_url"] = str(patch["base_url"]).strip()
            if patch.get("api_key"):
                prov["api_key"] = str(patch["api_key"]).strip()
            if patch.get("timeout"):
                try:
                    prov["timeout"] = float(patch["timeout"])
                except (TypeError, ValueError):
                    pass
            changed.append(name)

        # 可选：新增/更新模型映射
        model_patch = payload.get("models") or []
        if isinstance(model_patch, list):
            ov_models = overrides.setdefault("models", [])
            for m in model_patch:
                if not isinstance(m, dict) or not m.get("name"):
                    continue
                found = next(
                    (x for x in ov_models if isinstance(x, dict) and x.get("name") == m["name"]),
                    None,
                )
                if found is not None:
                    found.update(m)
                else:
                    ov_models.append(m)

        config_store.save(overrides)
        state.reload(merge_config(base_cfg, overrides))
        return {"ok": True, "changed": changed, "providers": list(state.providers.keys())}

    @app.delete("/v1/config")
    async def reset_config(request: Request):
        """清除所有 SQLite 覆盖，回到 config.yaml 的原始配置。"""
        _check_admin(request)
        config_store.save({})
        state.reload(merge_config(base_cfg, {}))
        return {"ok": True, "providers": list(state.providers.keys())}

    @app.post("/v1/config/reload")
    async def reload_config(request: Request):
        nonlocal base_cfg
        _check_admin(request)
        if config_path:
            base_cfg = load_config(config_path)
        state.reload(merge_config(base_cfg, config_store.load()))
        return {"ok": True, "providers": list(state.providers.keys())}

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
        return {
            "version": __version__,
            "providers": prov_status,
            "models": [
                {
                    "id": name,
                    "kind": state.model_kinds.get(name, "chat"),
                    "routes": [
                        {"provider": r.provider, "model": r.model, "weight": r.weight} for r in routes
                    ],
                }
                for name, routes in state.models.items()
            ],
        }

    @app.get("/v1/usage")
    async def usage(request: Request):
        _check_admin(request)
        snap = stats.snapshot()
        snap["persisted_total_calls"] = store.total_calls() if store is not None else 0
        return snap

    @app.get("/v1/usage/history")
    async def usage_history(request: Request, days: int = Query(7, ge=1, le=90)):
        _check_admin(request)
        return store.daily_summary(days) if store is not None else []

    @app.get("/v1/costs")
    async def costs(request: Request):
        _check_admin(request)
        if cost is None:
            raise HTTPException(status_code=503, detail="cost tracking disabled (no db_path)")
        return cost.summary()

    @app.get("/v1/requests")
    async def list_requests(request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        _check_admin(request)
        return reqlog.list(limit, offset) if reqlog is not None else []

    @app.get("/v1/requests/{req_id}")
    async def get_request(req_id: str, request: Request):
        _check_admin(request)
        if reqlog is None:
            raise HTTPException(status_code=503, detail="request logging disabled")
        entry = reqlog.get(req_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"request not found: {req_id}")
        return entry

    @app.post("/v1/requests/{req_id}/replay")
    async def replay(req_id: str, request: Request):
        _check_admin(request)
        if reqlog is None:
            raise HTTPException(status_code=503, detail="request logging disabled")
        entry = reqlog.get(req_id)
        if not entry or not entry.get("request"):
            raise HTTPException(status_code=404, detail=f"request not found: {req_id}")
        endpoint = entry["endpoint"]
        req = entry["request"]
        if endpoint == "/v1/chat/completions":
            return await _handle_chat(ChatRequest(**req))
        if endpoint == "/v1/embeddings":
            return await _handle_embeddings(EmbeddingsRequest(**req))
        raise HTTPException(status_code=400, detail=f"cannot replay endpoint: {endpoint}")

    # ---------- 推理接口 ----------
    @app.post("/v1/chat/completions")
    async def chat_completions(body: ChatRequest, request: Request):
        _check_auth(request)
        return await _handle_chat(body)

    @app.post("/v1/embeddings")
    async def embeddings(body: EmbeddingsRequest, request: Request):
        _check_auth(request)
        return await _handle_embeddings(body)

    @app.post("/v1/completions")
    async def completions(body: dict, request: Request):
        _check_auth(request)
        prompt = body.get("prompt")
        if isinstance(prompt, str):
            messages = [{"role": "user", "content": prompt}]
        elif isinstance(prompt, list):
            messages = [{"role": "user", "content": "".join(str(p) for p in prompt)}]
        else:
            raise HTTPException(status_code=400, detail="prompt must be a string or list")
        chat_body = {k: v for k, v in body.items() if k not in ("prompt",)}
        chat_body["messages"] = messages
        return await _handle_chat(ChatRequest(**chat_body))

    @app.post("/v1/audio/transcriptions")
    async def transcriptions(request: Request):
        _check_auth(request)
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

        cands = state.router.candidates(model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown model: {model}")

        ctx = {"endpoint": "/v1/audio/transcriptions"}

        async def fn(cand: Candidate):
            return await _forward_transcription(cand, model, content, filename, content_type, extra, ctx)

        return await _try_all(cands, model, fn)

    return app
