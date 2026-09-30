"""Tiny `/healthz` HTTP endpoint for container probes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

from fastapi import FastAPI
from fastapi.responses import JSONResponse

Check = Callable[[], Awaitable[bool]]


def create_health_app(
    worker_ok: Callable[[], bool], checks: dict[str, Check], *, disabled: bool
) -> FastAPI:
    app = FastAPI(title="huddle-agent", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        results = {name: await check() for name, check in checks.items()}
        alive = worker_ok()
        ok = alive and all(results.values())
        body: dict[str, object] = {
            "status": "ok" if ok else "degraded",
            "worker": alive,
            "agents_disabled": disabled,
            **results,
        }
        return JSONResponse(body, status_code=200 if ok else 503)

    return app
