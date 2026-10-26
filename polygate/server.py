"""FastAPI 网关服务：统一 /v1/chat/completions，支持流式透传与故障切换。"""

from __future__ import annotations

import os
import time
from contextlib import asynccontextmanager
from typing import Optional

import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__
from .config import load_config
from .errors import UpstreamError, is_retryable
from .providers import Provider
from .router import Candidate, Route, Router
from .schemas import ChatRequest
from .stats import Stats

_client: Optional[httpx.AsyncClient] = None
_stats = Stats()


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
    return providers, models


def create_app(config: Optional[str | dict] = None) -> FastAPI:
    global _client

    if isinstance(config, dict):
        cfg = config
    else:
        path = config or os.environ.get("POLYGATE_CONFIG", "config.example.yaml")
        cfg = load_config(path)

    providers, models = _build(cfg)
    router = Router(providers, models)
    auth = cfg.get("auth") or {}
    gateway_key = auth.get("api_key") or None
    admin_key = auth.get("admin_key") or gateway_key

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        global _client
        _client = httpx.AsyncClient(
            timeout=httpx.Timeout(120.0, connect=10.0),
            limits=httpx.Limits(max_connections=200),
        )
        try:
            yield
        finally:
            await _client.aclose()
            _client = None

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

    @app.get("/")
    async def index():
        return {
            "name": "polygate",
            "version": __version__,
            "docs": "/docs",
            "models": "/v1/models",
            "healthz": "/healthz",
        }

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
            {"id": name, "object": "model", "owned_by": routes[0].provider}
            for name, routes in models.items()
        ]
        return {"object": "list", "data": data}

    @app.get("/v1/usage")
    async def usage(request: Request):
        if admin_key and request.headers.get("authorization", "") != f"Bearer {admin_key}":
            raise HTTPException(status_code=401, detail="invalid admin key")
        return _stats.snapshot()

    @app.post("/v1/chat/completions")
    async def chat_completions(body: ChatRequest, request: Request):
        _check_auth(request)
        cands = router.candidates(body.model)
        if not cands:
            raise HTTPException(status_code=404, detail=f"unknown model: {body.model}")

        last: Optional[UpstreamError] = None
        for cand in cands:
            try:
                return await _call(cand, body)
            except UpstreamError as exc:
                last = exc
                if not is_retryable(exc.status):
                    break

        detail = f"all providers failed for model '{body.model}'"
        if last:
            detail += f": {last.message[:300]}"
        raise HTTPException(status_code=502, detail=detail)

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

    async def _call(cand: Candidate, body: ChatRequest):
        payload = body.model_dump(exclude_none=True)
        payload["model"] = cand.upstream_model
        if body.stream:
            payload.setdefault("stream_options", {"include_usage": True})
            return await _stream(cand, payload, body.model)
        return await _nonstream(cand, payload, body.model)

    async def _nonstream(cand: Candidate, payload: dict, model_name: str):
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        try:
            resp = await _client.post(cand.provider.chat_url, json=payload, headers=headers)
        except httpx.HTTPError as exc:
            _stats.record(model_name, cand.provider_name, "error")
            raise UpstreamError(None, str(exc))

        latency = (time.time() - start) * 1000
        if resp.status_code >= 400:
            _stats.record(model_name, cand.provider_name, "error", latency_ms=latency)
            raise UpstreamError(resp.status_code, resp.text[:500])

        try:
            data = resp.json()
        except Exception:
            data = {"raw": resp.text}
        usage = data.get("usage") or {}
        _stats.record(
            model_name,
            cand.provider_name,
            "ok",
            prompt=int(usage.get("prompt_tokens", 0) or 0),
            completion=int(usage.get("completion_tokens", 0) or 0),
            latency_ms=latency,
        )
        return JSONResponse(data)

    async def _stream(cand: Candidate, payload: dict, model_name: str):
        start = time.time()
        headers = cand.provider.headers(cand.api_key)
        req = _client.build_request("POST", cand.provider.chat_url, json=payload, headers=headers)
        try:
            resp = await _client.send(req, stream=True)
        except httpx.HTTPError as exc:
            _stats.record(model_name, cand.provider_name, "error")
            raise UpstreamError(None, str(exc))

        if resp.status_code >= 400:
            raw = await resp.aread()
            await resp.aclose()
            _stats.record(
                model_name,
                cand.provider_name,
                "error",
                latency_ms=(time.time() - start) * 1000,
            )
            raise UpstreamError(resp.status_code, raw[:500].decode("utf-8", "replace"))

        async def gen():
            try:
                async for chunk in resp.aiter_bytes():
                    yield chunk
            finally:
                await resp.aclose()

        _stats.record(model_name, cand.provider_name, "ok", latency_ms=(time.time() - start) * 1000)
        return StreamingResponse(gen(), media_type="text/event-stream")

    return app


app = create_app()
