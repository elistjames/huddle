"""FastAPI application factory, lifespan (migrate + bootstrap), middleware and error handlers."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from huddle_chat.api import internal, public
from huddle_chat.config import Settings
from huddle_chat.db import create_engine, is_persistence_failure, migrate
from huddle_chat.errors import ChatError
from huddle_chat.obs import TraceContext, current_trace, log_event
from huddle_chat.redis_ops import RedisOps
from huddle_chat.service import ChatService
from huddle_contracts.errors import ErrorBody, ErrorCode
from huddle_contracts.internal import SubmitMessageError

STARTUP_ATTEMPTS = 30
STARTUP_RETRY_SECONDS = 2.0
QUIET_PATHS = frozenset({"/healthz", "/readyz"})


class TraceMiddleware:
    """Continues (or starts) a W3C trace per request, echoes `traceparent`, logs the request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming: str | None = None
        for name, value in scope.get("headers", []):
            if name == b"traceparent":
                incoming = value.decode("latin-1")
                break
        ctx = TraceContext.continue_from(incoming)
        token = current_trace.set(ctx)
        status = 500
        started = time.perf_counter()

        async def send_with_trace(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = int(message["status"])
                MutableHeaders(scope=message).append("traceparent", ctx.traceparent)
            await send(message)

        try:
            await self.app(scope, receive, send_with_trace)
        finally:
            path = str(scope.get("path", ""))
            log_event(
                "http.request",
                level=logging.DEBUG if path in QUIET_PATHS else logging.INFO,
                latency_ms=(time.perf_counter() - started) * 1000,
                method=scope.get("method"),
                path=path,
                status=status,
            )
            current_trace.reset(token)


def _error_response(err: ChatError) -> JSONResponse:
    headers: dict[str, str] = {}
    if err.retry_after_ms is not None:
        headers["Retry-After"] = str(max(1, -(-err.retry_after_ms // 1000)))
    body = SubmitMessageError(error=err.body())
    return JSONResponse(body.model_dump(mode="json"), status_code=err.status, headers=headers)


async def _on_chat_error(_: Request, exc: Exception) -> JSONResponse:
    if not isinstance(exc, ChatError):  # pragma: no cover - registered for ChatError only
        raise exc
    return _error_response(exc)


async def _on_validation_error(_: Request, exc: Exception) -> JSONResponse:
    detail = "invalid request"
    if isinstance(exc, RequestValidationError):
        # Never echo the input: it may contain a message body.
        parts = [
            ".".join(str(p) for p in e.get("loc", ())) + ": " + str(e.get("msg", ""))
            for e in exc.errors()
        ]
        detail = "; ".join(parts)[:500] or detail
    return _error_response(ChatError(ErrorCode.VALIDATION, detail))


async def _on_unhandled(_: Request, exc: Exception) -> JSONResponse:
    if is_persistence_failure(exc):
        return _error_response(ChatError(ErrorCode.PERSISTENCE_UNAVAILABLE, "store unavailable"))
    log_event("unhandled_error", level=logging.ERROR, exc_info=True, error=type(exc).__name__)
    body = SubmitMessageError(error=ErrorBody.of(ErrorCode.INTERNAL, "internal error"))
    return JSONResponse(body.model_dump(mode="json"), status_code=500)


async def _start(service: ChatService, settings: Settings) -> None:
    """Migrate and bootstrap, retrying while Postgres comes up (compose/k8s start order)."""
    for attempt in range(1, STARTUP_ATTEMPTS + 1):
        try:
            if settings.migrate_on_startup:
                await migrate(service.engine)
            agent_id = await service.bootstrap()
        except Exception as exc:
            if attempt == STARTUP_ATTEMPTS or not (
                is_persistence_failure(exc)
                or (isinstance(exc, ChatError) and exc.code == ErrorCode.PERSISTENCE_UNAVAILABLE)
            ):
                raise
            log_event("startup.waiting_for_postgres", level=logging.WARNING, attempt=attempt)
            await asyncio.sleep(STARTUP_RETRY_SECONDS)
        else:
            log_event("startup.ready", session_id=settings.session_id, actor_id=agent_id)
            return


def create_app(settings: Settings | None = None) -> FastAPI:
    cfg = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(cfg.postgres_dsn)
        redis = RedisOps.connect(cfg.redis_url)
        service = ChatService(engine, redis, cfg)
        try:
            await _start(service, cfg)
            app.state.service = service
            yield
        finally:
            await redis.close()
            await engine.dispose()

    app = FastAPI(title="Huddle chat service", version="0.1.0", lifespan=lifespan)
    app.state.settings = cfg
    app.add_middleware(TraceMiddleware)
    if cfg.cors_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(cfg.cors_origins),
            allow_methods=["GET", "POST", "PATCH", "OPTIONS"],
            allow_headers=["authorization", "content-type", "traceparent"],
            expose_headers=["traceparent", "retry-after"],
        )
    app.add_exception_handler(ChatError, _on_chat_error)
    app.add_exception_handler(RequestValidationError, _on_validation_error)
    app.add_exception_handler(Exception, _on_unhandled)
    app.include_router(public)
    app.include_router(internal)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz(request: Request) -> JSONResponse:
        service: ChatService | None = getattr(request.app.state, "service", None)
        if service is None:
            return JSONResponse({"status": "starting"}, status_code=503)
        checks: dict[str, str] = {}
        try:
            async with service.tx() as conn:
                await conn.exec_driver_sql("SELECT 1")
            checks["postgres"] = "ok"
        except ChatError:
            checks["postgres"] = "unavailable"
        try:
            await service.redis.ping()
            checks["redis"] = "ok"
        except Exception:
            checks["redis"] = "unavailable"
        ok = all(v == "ok" for v in checks.values())
        return JSONResponse({"status": "ok" if ok else "degraded", **checks}, 200 if ok else 503)

    return app
