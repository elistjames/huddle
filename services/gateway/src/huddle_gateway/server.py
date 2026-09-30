"""uvicorn server with graceful draining on SIGTERM (Deep Dive §2 "Server-side draining").

uvicorn's own shutdown closes every websocket with 1012 at once. Instead, the first SIGTERM/SIGINT
drains: every client gets `server.draining {reconnect_after_ms}` with a jittered delay and its
socket is closed, and only then does uvicorn shut down. A second signal forces the normal exit.
"""

from __future__ import annotations

import asyncio
import logging
from types import FrameType

import uvicorn
from fastapi import FastAPI

from huddle_gateway.app import create_app, gateway_of
from huddle_gateway.config import Settings
from huddle_gateway.gateway import Gateway
from huddle_gateway.logs import configure, log_event


class DrainingServer(uvicorn.Server):
    def __init__(self, config: uvicorn.Config, gateway: Gateway) -> None:
        super().__init__(config)
        self.gateway = gateway
        self._drain_task: asyncio.Task[None] | None = None

    def handle_exit(self, sig: int, frame: FrameType | None) -> None:
        if self._drain_task is not None or self.should_exit:
            super().handle_exit(sig, frame)  # second signal: stop now
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            super().handle_exit(sig, frame)
            return
        self._drain_task = loop.create_task(self._drain_then_exit(sig))

    async def _drain_then_exit(self, sig: int) -> None:
        try:
            await self.gateway.drain()
        except Exception:
            log_event("gateway.drain_failed", level=logging.ERROR)
        super().handle_exit(sig, None)


def build_server(settings: Settings, app: FastAPI | None = None) -> DrainingServer:
    app = app or create_app(settings)
    config = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        log_config=None,
        access_log=False,
        ws_max_size=settings.max_frame_bytes,
        timeout_graceful_shutdown=int(settings.close_grace_s * 2) + 1,
        lifespan="on",
    )
    return DrainingServer(config, gateway_of(app))


def main() -> None:
    configure()
    settings = Settings.from_env()
    log_event("gateway.boot", gateway_id=settings.gateway_id, port=settings.port)
    build_server(settings).run()
