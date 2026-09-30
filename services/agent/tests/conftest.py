from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Iterator

import pytest
from agent_pg import create_test_database, drop_test_database, engine_for, reset_and_seed
from agent_testkit import make_executor, require_env
from redis.asyncio import Redis
from sqlalchemy.ext.asyncio import AsyncEngine

from huddle_agent.clock import FakeClock
from huddle_agent.config import Settings
from huddle_agent.memory import MemoryWorld
from huddle_agent.models.fake import ScriptedModel
from huddle_agent.runner import RunExecutor


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def world(clock: FakeClock) -> MemoryWorld:
    return MemoryWorld(clock=clock)


@pytest.fixture
def model(clock: FakeClock) -> ScriptedModel:
    return ScriptedModel(clock=clock)


@pytest.fixture
def settings() -> Settings:
    return Settings(worker_id="worker-1")


@pytest.fixture
def executor(world: MemoryWorld, model: ScriptedModel, settings: Settings) -> RunExecutor:
    return make_executor(world, model, settings)


# ---- real stores (integration) -------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    """A throwaway database with the tests-only §4 schema (the chat service owns the real
    migrations). Other services' tests may share the server, so never touch `postgres`."""
    base = require_env("HUDDLE_TEST_POSTGRES_DSN")
    dsn = create_test_database(base)
    yield dsn
    drop_test_database(base, dsn)


@pytest.fixture
async def engine(pg_dsn: str) -> AsyncIterator[AsyncEngine]:
    eng = engine_for(pg_dsn)
    await reset_and_seed(eng)
    yield eng
    await eng.dispose()


@pytest.fixture
async def redis_client() -> AsyncIterator[Redis]:
    url = require_env("HUDDLE_TEST_REDIS_URL")
    client: Redis = Redis.from_url(url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    yield client
    await client.aclose()


@pytest.fixture
async def redis_settings(redis_client: Redis) -> AsyncIterator[Settings]:
    """Per-test key names so parallel test runs sharing one Redis never collide; the default
    names (the contract's) are asserted separately."""
    tag = uuid.uuid4().hex[:8]
    s = Settings(
        worker_id=f"worker-{tag}",
        triggers_stream=f"test:{tag}:agent:triggers",
        triggers_group="agent-workers",
        timers_zset=f"test:{tag}:agent:timers",
        stream_block_ms=50,
        reclaim_idle_s=1.0,
        timer_poll_s=0.05,
        reconcile_s=0.3,
        busy_retry_s=0.05,
    )
    yield s
    await redis_client.delete(s.triggers_stream, s.timers_zset)
