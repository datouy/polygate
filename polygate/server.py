"""FastAPI 网关服务。

统一 /v1/chat/completions、/v1/embeddings、/v1/audio/transcriptions，
支持流式透传、多 Key 轮询、权重负载均衡、故障切换、令牌桶限流（内存/Redis）、
熔断器、SQLite 持久化、请求日志与重放、按 Key 成本记账、Prometheus 指标与管理面板。
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
from .circuit import CircuitBreaker
from .config import load_config
from .cost import CostTracker, compute_cost
from .errors import UpstreamError, is_retryable
from .metrics import Metrics
from .providers import Provider
from .ratelimit import RateLimiter
from .redislimiter import RedisRateLimiter
from .requestlog import RequestLog
from .router import Candidate, Route, Router
from .schemas import ChatRequest, EmbeddingsRequest
from .stats import Stats
from .store import UsageStore

_client: Optional[httpx.AsyncClient] = None
_stats = Stats()
_metrics = Metrics()
_store: Optional[UsageStore] = None
_reqlog: Optional[RequestLog] = None
_cost: Optional[CostTracker] = None


def _key_label(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 8:
        return "****"
    return f"{key[:4]}...{key[-4:]}"


def _build(cfg: dict):
    providers: dict[str, Provider] = {}
    for name, p in (cfg.get("providers") or {}).items():
        base_url = p.get("base_url")
        if not base_url:
            continue
        keys = list(p.get("api_keys") or [])
        if p.get("api_key"):
            keys.insert(0, p["api_key"])
        keys = [k for k in keys if k]
        providers[name] = Provider(
            name=name,
            base_url=base_url,
            api_keys=keys,
            timeout=float(p.get("timeout", 120.0)),
        )

    models: dict[str, list[Route]] = {}
    model_kinds: dict[str, str] = {}
    for m in (cfg.get("models") or []):
        name = m.get("name")
        routes = [
            Route(
                provider=r.get("provider"),
                model=r.get("model"),
                weight=float(r.get("weight", 1.0)),
            )
            for r in (m.get("providers") or [])
            if r.get("provider") and r.get("model")
        ]
        if name and routes:
            models[name] = routes
            model_kinds[name] = m.get("kind", "chat")

    rate_limits: dict[str, tuple[float, float]] = {}
    for name, p in (cfg.get("providers") or {}).items():
        rl = p.get("rate_limit")
        if rl:
            rate_limits[name] = (float(rl.get("rate", 10.0)), float(rl.get("burst", 10.0)))

    pricing = cfg.get("pricing") or {}
    return providers, models, model_kinds, rate_limits, pricing


def _build_limiter(redis_cfg):
    if not redis_cfg or not redis_cfg.get("url"):
        return RateLimiter()
    try:
        return RedisRateLimiter(redis_cfg["url"])
    except Exception:
        return RateLimiter()


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
        cfg = config
    else:
        path = _resolve_config_path(config)
        cfg = load_config(path)

    providers, models, model_kinds, rate_limits, pricing = _build(cfg)
    router = Router(providers, models)
    auth = cfg.get("auth") or {}
    gateway_key = auth.get("api_key") or None
    admin_key = auth.get("admin_key") or gateway_key

    health_cfg = cfg.get("health") or {}
    health_enabled = bool(health_cfg.get("enabled", True))
    health_interval = float(health_cfg.get("interval", 30.0))
    circuits: dict[str, CircuitBreaker] = {
        name: CircuitBreaker(
            failure_threshold=int(health_cfg.get("failure_threshold", 3)),
            cooldown_sec=float(health_cfg.get("cooldown", 60.0)),
        )
        for name in providers
    }
    limiter = _build_limiter(cfg.get("redis"))

    db_path = cfg.get("db_path", "polygate.db") or None

    def _record_stats(model, provider, status, prompt=0, completion=0, latency_ms=0.0, key_label=None):
        _stats.record(model, provider, status, prompt, completion, latency_ms)
        if _store is not None:
            _store.record(model, provider, status, prompt, completion, latency_ms)
        if _cost is not None and status == "ok" and key_label:
            cost = compute_cost(prompt, completion, pricing, model)
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
                req_id=ctx["req_id"],
                endpoint=ctx["endpoint"],
                model=model_name,
                provider=provider,
                key_label=key_label,
                status=status,
                status_code=status_code,
                latency_ms=latency,
                request=request,
                response=response,
                error=error,
            )
        except Exception:
            pass

    async def _ping_provider(prov: Provider) -> bool:
        if not prov.api_keys:
            return False
        try:
            resp = await _client.get(prov.models_url, headers=prov.headers(prov.api_keys[0]), timeout=5.0)
            return resp.status_code < 500
        except httpx.HTTPError:
            return False

    async def _health_loop():
        while True:
            await asyncio.sleep(health_interval)
            for name, prov in providers.items():
                ok = await _ping_provider(prov)
                if ok:
                    circuits[name].record_success()
                else:
                    circuits[name].record_failure()

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
        health_task = asyncio.create_task(_health_loop()) if health_enabled else None
        try:
            yield
        finally:
            if health_task:
                health_task.cancel()
            await _client.aclose()
            _client = None
            for obj in (_store, _reqlog, _cost):
                if obj is not None:
                    obj.close()
            _store = _reqlog = _cost = None

    app = FastAPI(
        title="polygate",
        description="国产大模型统一 API 网关（OpenAI 兼容）",
        version=__version__,
        lifespan=lifespan,
    )

    def _check_auth(request: Request) -> None:
        if not gateway_key:
            return
        if request.headers.get("authorization", "") != f"Bearer {gateway_key}":
            raise HTTPException(status_code=401, detail="invalid gateway api key")

    def _check_admin(request: Request) -> None:
        if admin_key and request.headers.get("authorization", "") != f"Bearer {admin_key}":
            raise HTTPException(status_code=401, detail="invalid admin key")

    async def _forward_nonstream(cand: Candidate, payload: dict, model_name: str, url: str, ctx: dict) -> dict:
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        key_label = _key_label(cand.api_key)
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
        key_label = _key_label(cand.api_key)
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
        key_label = _key_label(cand.api_key)
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
        """按候选顺序尝试；处理熔断、限流、故障切换。"""
        last: Optional[UpstreamError] = None
        attempted = False
        rate_limited = False
        circuit_open = False
        for cand in cands:
            if not circuits[cand.provider_name].allow():
                circuit_open = True
                continue
            rl = rate_limits.get(cand.provider_name)
            if rl:
                ok = await asyncio.to_thread(limiter.allow, f"{cand.provider_name}:{cand.api_key}", rl[0], rl[1])
                if not ok:
                    rate_limited = True
                    continue
            attempted = True
            try:
                result = await fn(cand)
                circuits[cand.provider_name].record_success()
                return result
            except UpstreamError as exc:
                last = exc
                if is_retryable(exc.status):
                    circuits[cand.provider_name].record_failure()
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
        cands = router.candidates(body.model)
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
        cands = router.candidates(body.model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown embedding model: {body.model}")
        payload = body.model_dump(exclude_none=True)
        ctx = {"req_id": uuid.uuid4().hex, "endpoint": "/v1/embeddings"}

        async def fn(cand: Candidate):
            payload["model"] = cand.upstream_model
            return JSONResponse(await _forward_nonstream(cand, payload, body.model, cand.provider.embeddings_url, ctx))

        return await _try_all(cands, body.model, fn)

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
            "providers": len(providers),
            "models": len(models),
        }

    @app.get("/metrics")
    async def metrics():
        gauges = [("polygate_up", {}, 1.0)]
        for name, c in circuits.items():
            state = 0 if c.state == "closed" else (1 if c.state == "open" else 2)
            gauges.append(("polygate_provider_circuit_state", {"provider": name}, state))
        return Response(
            content=_metrics.render(extra_gauges=gauges),
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @app.get("/v1/models")
    async def list_models(request: Request):
        _check_auth(request)
        data = [
            {"id": name, "object": "model", "owned_by": routes[0].provider, "kind": model_kinds.get(name, "chat")}
            for name, routes in models.items()
        ]
        return {"object": "list", "data": data}

    @app.get("/v1/status")
    async def status(request: Request):
        _check_admin(request)
        prov_status = {}
        for name, c in circuits.items():
            prov_status[name] = {
                **c.snapshot(),
                "keys": len(providers[name].api_keys),
                "rate_limit": rate_limits.get(name),
            }
        return {
            "version": __version__,
            "providers": prov_status,
            "models": [
                {
                    "id": name,
                    "kind": model_kinds.get(name, "chat"),
                    "routes": [
                        {"provider": r.provider, "model": r.model, "weight": r.weight} for r in routes
                    ],
                }
                for name, routes in models.items()
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

        cands = router.candidates(model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown model: {model}")

        ctx = {"req_id": uuid.uuid4().hex, "endpoint": "/v1/audio/transcriptions"}

        async def fn(cand: Candidate):
            return JSONResponse(await _forward_transcription(cand, model, content, filename, content_type, extra, ctx))

        return await _try_all(cands, model, fn)

    return app


app = create_app()
