"""Entry point: `python -m huddle_agent` runs the orchestrator worker plus `/healthz`."""

from __future__ import annotations

import asyncio
import contextlib
import signal

import uvicorn
from redis.asyncio import Redis
from redis.exceptions import RedisError

from huddle_agent import logs
from huddle_agent.chat_client import HttpChatClient
from huddle_agent.config import Settings
from huddle_agent.health import create_health_app
from huddle_agent.models.ollama import OllamaModelClient
from huddle_agent.store import PostgresStore
from huddle_agent.worker import Worker


async def main(settings: Settings | None = None) -> None:
    s = settings or Settings.from_env()
    logs.configure()
    # decode_responses=True is required: the worker reads stream fields as str.
    # redis-py's from_url/ping stubs carry unknown callback types; the values are plain.
    redis = Redis.from_url(s.redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    store = PostgresStore.from_dsn(s.postgres_dsn)
    chat = HttpChatClient(s.chat_url, s.service_token)
    models = OllamaModelClient(s.ollama_url)
    worker = Worker(settings=s, redis=redis, store=store, chat=chat, models=models)

    async def redis_ok() -> bool:
        try:
            return bool(await redis.ping())  # pyright: ignore[reportUnknownMemberType]
        except RedisError:
            return False

    app = create_health_app(
        worker.healthy,
        {"redis": redis_ok, "postgres": store.ping},
        disabled=s.agents_disabled,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, host=s.health_host, port=s.health_port, log_level="warning")
    )
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, worker.stop)

    health = asyncio.create_task(server.serve())
    try:
        await worker.run()
    finally:
        server.should_exit = True
        with contextlib.suppress(Exception):
            await health
        await chat.aclose()
        await store.dispose()
        await redis.aclose()


if __name__ == "__main__":
    asyncio.run(main())
