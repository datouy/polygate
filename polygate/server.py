"""FastAPI 网关服务。

统一 /v1/chat/completions 与 /v1/embeddings，支持流式透传、多 Key 轮询、
权重负载均衡、故障切换、令牌桶限流、熔断器、SQLite 持久化与管理面板。
"""

from __future__ import annotations

import asyncio
import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from . import __version__
from .admin import ADMIN_HTML
from .circuit import CircuitBreaker
from .config import load_config
from .errors import UpstreamError, is_retryable
from .providers import Provider
from .ratelimit import RateLimiter
from .router import Candidate, Route, Router
from .schemas import ChatRequest, EmbeddingsRequest
from .stats import Stats
from .store import UsageStore

_client: Optional[httpx.AsyncClient] = None
_stats = Stats()
_store: Optional[UsageStore] = None


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

    return providers, models, model_kinds, rate_limits


def create_app(config: Optional[str | dict] = None) -> FastAPI:
    global _client, _store

    if isinstance(config, dict):
        cfg = config
    else:
        path = config or os.environ.get("POLYGATE_CONFIG", "config.example.yaml")
        cfg = load_config(path)

    providers, models, model_kinds, rate_limits = _build(cfg)
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
    limiter = RateLimiter()

    db_path = cfg.get("db_path", "polygate.db") or None

    def _record_stats(model, provider, status, prompt=0, completion=0, latency_ms=0.0):
        _stats.record(model, provider, status, prompt, completion, latency_ms)
        if _store is not None:
            _store.record(model, provider, status, prompt, completion, latency_ms)

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
        global _client, _store
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=200),
        )
        if db_path:
            _store = UsageStore(db_path)
        health_task = asyncio.create_task(_health_loop()) if health_enabled else None
        try:
            yield
        finally:
            if health_task:
                health_task.cancel()
            await _client.aclose()
            _client = None
            if _store is not None:
                _store.close()
                _store = None

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

    async def _forward_nonstream(cand: Candidate, payload: dict, model_name: str, url: str) -> dict:
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        try:
            resp = await _client.post(url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error")
            raise UpstreamError(None, str(exc))

        latency = (time.time() - start) * 1000
        if resp.status_code >= 400:
            _record_stats(model_name, cand.provider_name, "error", latency_ms=latency)
            raise UpstreamError(resp.status_code, resp.text[:500])

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        usage = data.get("usage") or {}
        _record_stats(
            model_name,
            cand.provider_name,
            "ok",
            prompt=int(usage.get("prompt_tokens", 0) or 0),
            completion=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=latency,
        )
        return data

    async def _stream(cand: Candidate, payload: dict, model_name: str):
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        req = _client.build_request("POST", cand.provider.chat_url, json=payload, headers=headers)
        try:
            resp = await _client.send(req, stream=True)
        except httpx.HTTPError as exc:
            _record_stats(model_name, cand.provider_name, "error")
            raise UpstreamError(None, str(exc))

        if resp.status_code >= 400:
            raw = await resp.aread()
            await resp.aclose()
            _record_stats(model_name, cand.provider_name, "error", latency_ms=(time.time() - start) * 1000)
            raise UpstreamError(resp.status_code, raw[:500].decode("utf-8", "replace"))

        async def gen():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        _record_stats(model_name, cand.provider_name, "ok", latency_ms=(time.time() - start) * 1000)
        return StreamingResponse(gen(), media_type="text/event-stream")

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
            if rl and not limiter.allow(f"{cand.provider_name}:{cand.api_key}", rl[0], rl[1]):
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

    @app.post("/v1/chat/completions")
    async def chat_completions(body: ChatRequest, request: Request):
        _check_auth(request)
        cands = router.candidates(body.model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown model: {body.model}")

        payload = body.model_dump(exclude_none=True)
        if body.stream:
            payload.setdefault("stream_options", {"include_usage": True})

        async def fn(cand: Candidate):
            payload["model"] = cand.upstream_model
            if body.stream:
                return await _stream(cand, payload, body.model)
            return JSONResponse(await _forward_nonstream(cand, payload, body.model, cand.provider.chat_url))

        return await _try_all(cands, body.model, fn)

    @app.post("/v1/embeddings")
    async def embeddings(body: EmbeddingsRequest, request: Request):
        _check_auth(request)
        cands = router.candidates(body.model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown embedding model: {body.model}")

        payload = body.model_dump(exclude_none=True)

        async def fn(cand: Candidate):
            payload["model"] = cand.upstream_model
            return JSONResponse(await _forward_nonstream(cand, payload, body.model, cand.provider.embeddings_url))

        return await _try_all(cands, body.model, fn)

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
        return await chat_completions(ChatRequest(**chat_body), request)

    return app


app = create_app()
