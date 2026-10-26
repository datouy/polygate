"""FastAPI 网关服务。

统一 /v1/chat/completions、/v1/embeddings、/v1/audio/transcriptions，
支持流式透传、多 Key 轮询、权重负载均衡、故障切换、令牌桶限流（内存/Redis）、
熔断器、SQLite 持久化、请求日志与重放、按 Key 成本记账、Prometheus 指标与管理面板，
并支持在管理面板中在线修改配置（base_url / api_key）后热重载，无需重启。
"""

from __future__ import annotations

import asyncio
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
from .errors import UpstreamError, is_retryable
from .metrics import Metrics
from .requestlog import RequestLog
from .router import Candidate
from .runtime import RuntimeState, mask_key
from .schemas import ChatRequest, EmbeddingsRequest
from .stats import Stats
from .store import UsageStore

_client: Optional[httpx.AsyncClient] = None
_stats = Stats()
_metrics = Metrics()
_store: Optional[UsageStore] = None
_reqlog: Optional[RequestLog] = None
_cost: Optional[CostTracker] = None


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


def create_app(config: Optional[str | dict] = None) -> FastAPI:
    global _client, _store, _reqlog, _cost

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
        _stats.record(model, provider, status, prompt, completion, latency_ms)
        if _store is not None:
            _store.record(model, provider, status, prompt, completion, latency_ms)
        if _cost is not None and status == "ok" and key_label:
            cost = compute_cost(prompt, completion, state.pricing, model)
            if cost > 0:
                _cost.add(key_label, model, prompt, completion, cost)
        _metrics.inc("polygate_requests_total", {"model": model, "provider": provider, "status": status})
        if status == "error":
            _metrics.inc("polygate_errors_total", {"model": model})
        if prompt:
            _metrics.inc("polygate_tokens_total", {"model": model, "type": "prompt"}, prompt)
        if completion:
            _metrics.inc("polygate_tokens_total", {"model": model, "type": "completion"}, completion)
        if latency_ms:
            _metrics.observe("polygate_request_duration_seconds", latency_ms / 1000.0, {"model": model})

    def _log_request(ctx, provider, model_name, key_label, status, status_code, latency, request, response, error):
        if _reqlog is None:
            return
        try:
            _reqlog.log(
                req_id=ctx["req_id"], endpoint=ctx["endpoint"], model=model_name, provider=provider,
                key_label=key_label, status=status, status_code=status_code, latency_ms=latency,
                request=request, response=response, error=error,
            )
        except Exception:
            pass

    # ---------- 健康检查 ----------
    async def _ping_provider(prov) -> bool:
        if not prov.api_keys:
            return False
        try:
            resp = await _client.get(prov.models_url, headers=prov.headers(prov.api_keys[0]), timeout=5.0)
            return resp.status_code < 500
        except httpx.HTTPError:
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
                    if ok:
                        circuit.record_success()
                    else:
                        circuit.record_failure()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _client, _store, _reqlog, _cost
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=200),
        )
        if db_path:
            _store = UsageStore(db_path)
            _reqlog = RequestLog(db_path)
            _cost = CostTracker(db_path)
        health_task = asyncio.create_task(_health_loop())
        try:
            yield
        finally:
            health_task.cancel()
            await _client.aclose()
            _client = None
            for obj in (_store, _reqlog, _cost):
                if obj is not None:
                    obj.close()
            _store = _reqlog = _cost = None
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
    async def _forward_nonstream(cand: Candidate, payload: dict, model_name: str, url: str, ctx: dict) -> dict:
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        key_label = mask_key(cand.api_key)
        try:
            resp = await _client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, payload, None, str(exc))
            raise UpstreamError(None, str(exc))

        latency = (time.time() - start) * 1000
        if resp.status_code >= 400:
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, payload, None, resp.text[:500])
            raise UpstreamError(resp.status_code, resp.text[:500])

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        usage = data.get("usage") or {}
        prompt = int(usage.get("prompt_tokens", 0) or 0)
        completion = int(usage.get("completion_tokens", 0) or 0)
        _record_stats(model_name, cand.provider_name, "ok", prompt=prompt, completion=completion, latency_ms=latency, key_label=key_label)
        _log_request(ctx, cand.provider_name, model_name, key_label, "ok", 200, latency, payload, data, None)
        return data

    async def _stream(cand: Candidate, payload: dict, model_name: str, ctx: dict):
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        key_label = mask_key(cand.api_key)
        req = _client.build_request("POST", cand.provider.chat_url, json=payload, headers=headers)
        try:
            resp = await _client.send(req, stream=True)
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, payload, None, str(exc))
            raise UpstreamError(None, str(exc))

        if resp.status_code >= 400:
            raw = await resp.aread()
            await resp.aclose()
            latency = (time.time() - start) * 1000
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, payload, None, raw[:500].decode("utf-8", "replace"))
            raise UpstreamError(resp.status_code, raw[:500].decode("utf-8", "replace"))

        async def gen():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        _record_stats(model_name, cand.provider_name, "ok", latency_ms=(time.time() - start) * 1000, key_label=key_label)
        _log_request(ctx, cand.provider_name, model_name, key_label, "ok", 200, (time.time() - start) * 1000, payload, {"stream": True}, None)
        return StreamingResponse(gen(), media_type="text/event-stream")

    async def _forward_transcription(cand: Candidate, model_name: str, content: bytes, filename: str, content_type: str, extra: dict, ctx: dict):
        start = time.time()
        key_label = mask_key(cand.api_key)
        headers = {}
        if cand.api_key:
            headers["Authorization"] = f"Bearer {cand.api_key}"
        payload_data = {**extra, "model": cand.upstream_model}
        files = {"file": (filename, content, content_type)}
        try:
            resp = await _client.post(cand.provider.transcriptions_url, data=payload_data, files=files, headers=headers)
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error", key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", None, 0.0, {"filename": filename, **extra}, None, str(exc))
            raise UpstreamError(None, str(exc))

        latency = (time.time() - start) * 1000
        if resp.status_code >= 400:
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency, key_label=key_label)
            _log_request(ctx, cand.provider_name, model_name, key_label, "error", resp.status_code, latency, {"filename": filename, **extra}, None, resp.text[:500])
            raise UpstreamError(resp.status_code, resp.text[:500])

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        _record_stats(model_name, cand.provider_name, "ok", latency_ms=latency, key_label=key_label)
        _log_request(ctx, cand.provider_name, model_name, key_label, "ok", 200, latency, {"filename": filename, **extra}, data, None)
        return data

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
                if is_retryable(exc.status) and circuit is not None:
                    circuit.record_failure()
                if not is_retryable(exc.status):
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
        payload = body.model_dump(exclude_none=True)
        if body.stream:
            payload.setdefault("stream_options", {"include_usage": True})
        ctx = {"req_id": uuid.uuid4().hex, "endpoint": "/v1/chat/completions"}

        async def fn(cand: Candidate):
            payload["model"] = cand.upstream_model
            if body.stream:
                return await _stream(cand, payload, body.model, ctx)
            return JSONResponse(await _forward_nonstream(cand, payload, body.model, cand.provider.chat_url, ctx))

        return await _try_all(cands, body.model, fn)

    async def _handle_embeddings(body: EmbeddingsRequest):
        cands = state.router.candidates(body.model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown embedding model: {body.model}")
        payload = body.model_dump(exclude_none=True)
        ctx = {"req_id": uuid.uuid4().hex, "endpoint": "/v1/embeddings"}

        async def fn(cand: Candidate):
            payload["model"] = cand.upstream_model
            return JSONResponse(await _forward_nonstream(cand, payload, body.model, cand.provider.embeddings_url, ctx))

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

    @app.get("/metrics")
    async def metrics():
        gauges = [("polygate_up", {}, 1.0)]
        for name, c in state.circuits.items():
            s = 0 if c.state == "closed" else (1 if c.state == "open" else 2)
            gauges.append(("polygate_provider_circuit_state", {"provider": name}, s))
        return Response(
            content=_metrics.render(extra_gauges=gauges),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/v1/models")
    async def list_models(request: Request):
        _check_auth(request)
        data = [
            {"id": name, "object": "model", "owned_by": routes[0].provider, "kind": state.model_kinds.get(name, "chat")}
            for name, routes in state.models.items()
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
        snap = _stats.snapshot()
        snap["persisted_total_calls"] = _store.total_calls() if _store is not None else 0
        return snap

    @app.get("/v1/usage/history")
    async def usage_history(request: Request, days: int = Query(7, ge=1, le=90)):
        _check_admin(request)
        return _store.daily_summary(days) if _store is not None else []

    @app.get("/v1/costs")
    async def costs(request: Request):
        _check_admin(request)
        if _cost is None:
            raise HTTPException(status_code=503, detail="cost tracking disabled (no db_path)")
        return _cost.summary()

    @app.get("/v1/requests")
    async def list_requests(request: Request, limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)):
        _check_admin(request)
        return _reqlog.list(limit, offset) if _reqlog is not None else []

    @app.get("/v1/requests/{req_id}")
    async def get_request(req_id: str, request: Request):
        _check_admin(request)
        if _reqlog is None:
            raise HTTPException(status_code=503, detail="request logging disabled")
        entry = _reqlog.get(req_id)
        if not entry:
            raise HTTPException(status_code=404, detail=f"request not found: {req_id}")
        return entry

    @app.post("/v1/requests/{req_id}/replay")
    async def replay(req_id: str, request: Request):
        _check_admin(request)
        if _reqlog is None:
            raise HTTPException(status_code=503, detail="request logging disabled")
        entry = _reqlog.get(req_id)
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

        ctx = {"req_id": uuid.uuid4().hex, "endpoint": "/v1/audio/transcriptions"}

        async def fn(cand: Candidate):
            return JSONResponse(await _forward_transcription(cand, model, content, filename, content_type, extra, ctx))

        return await _try_all(cands, model, fn)

    return app


app = create_app()
