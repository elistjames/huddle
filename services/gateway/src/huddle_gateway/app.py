"""FastAPI application: `/healthz` and the WebSocket endpoint `/ws` (Deep Dive §6)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket
from fastapi.responses import JSONResponse
from redis.asyncio import Redis

from huddle_gateway.config import Settings
from huddle_gateway.gateway import Gateway


def create_app(
    settings: Settings,
    *,
    redis: Redis | None = None,
    chat_transport: httpx.AsyncBaseTransport | None = None,
) -> FastAPI:
    gateway = Gateway(settings, redis=redis, chat_transport=chat_transport)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
        await gateway.start()
        try:
            yield
        finally:
            await gateway.stop()

    app = FastAPI(title="Huddle gateway", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.gateway = gateway

    @app.get("/healthz")
    async def healthz() -> JSONResponse:  # pyright: ignore[reportUnusedFunction]
        body = {
            "status": "draining" if gateway.draining else "ok",
            "gateway_id": settings.gateway_id,
            "connections": len(gateway.connections),
        }
        # 503 while draining so the load balancer stops routing new sockets here (§7).
        return JSONResponse(body, status_code=503 if gateway.draining else 200)

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:  # pyright: ignore[reportUnusedFunction]
        await gateway.handle(ws)

    return app


def gateway_of(app: FastAPI) -> Gateway:
    gw: Gateway = app.state.gateway
    return gw
