"""Fixtures for chat service tests.

Store-backed fixtures use real Postgres/Redis from HUDDLE_TEST_POSTGRES_DSN/HUDDLE_TEST_REDIS_URL
(`eval "$(scripts/dev-stores.sh)"`). Without them the tests skip, or fail when
HUDDLE_REQUIRE_STORES=1 (CI). Each run migrates a throwaway database; every test gets its own
session id so shared Redis keys (streams, registry) never collide with other runs.
"""

from __future__ import annotations

import asyncio
import os
import secrets
from collections.abc import AsyncIterator, Iterator
from typing import NoReturn

import pytest
import sqlalchemy as sa
from chat_testkit import JWT_SECRET, SERVICE_TOKEN, Clock, FakeGateways
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from huddle_chat.config import Settings, to_async_dsn
from huddle_chat.db import create_engine, migrate
from huddle_chat.redis_ops import RedisOps
from huddle_chat.service import ChatService

REQUIRE_STORES = os.environ.get("HUDDLE_REQUIRE_STORES") == "1"
TABLES = (
    "refresh_tokens, audit_events, agent_budgets, session_summaries, messages, agent_runs, "
    "agents, session_memberships, chat_sessions, users"
)


def unavailable(reason: str) -> NoReturn:
    if REQUIRE_STORES:
        pytest.fail(f"{reason} (HUDDLE_REQUIRE_STORES=1)")
    pytest.skip(reason)


# ---- Postgres ----------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def pg_dsn() -> Iterator[str]:
    admin = os.environ.get("HUDDLE_TEST_POSTGRES_DSN")
    if not admin:
        unavailable("HUDDLE_TEST_POSTGRES_DSN is not set")
    name = f"huddle_chat_test_{secrets.token_hex(4)}"
    url = make_url(admin)
    dsn = url.set(drivername="postgresql", database=name).render_as_string(hide_password=False)

    async def admin_sql(statement: str) -> None:
        eng = create_async_engine(
            to_async_dsn(admin),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
            connect_args={"timeout": 5},
        )
        try:
            async with eng.connect() as conn:
                await conn.exec_driver_sql(statement)
        finally:
            await eng.dispose()

    async def create() -> None:
        await admin_sql(f'CREATE DATABASE "{name}"')
        engine = create_engine(dsn)
        try:
            await migrate(engine)
        finally:
            await engine.dispose()

    try:
        asyncio.run(create())
    except (OSError, DBAPIError) as exc:
        unavailable(f"Postgres unreachable: {exc}")
    yield dsn
    asyncio.run(admin_sql(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))


@pytest.fixture
async def engine(pg_dsn: str) -> AsyncIterator[AsyncEngine]:
    eng = create_engine(pg_dsn, pool_size=20)
    async with eng.begin() as conn:
        await conn.execute(sa.text(f"TRUNCATE {TABLES} RESTART IDENTITY CASCADE"))
    yield eng
    await eng.dispose()


# ---- Redis -------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def redis_url() -> str:
    url = os.environ.get("HUDDLE_TEST_REDIS_URL")
    if not url:
        unavailable("HUDDLE_TEST_REDIS_URL is not set")
    return url


@pytest.fixture
async def redis_ops(redis_url: str) -> AsyncIterator[RedisOps]:
    ops = RedisOps.connect(redis_url)
    try:
        await ops.ping()
    except (OSError, RedisError) as exc:
        unavailable(f"Redis unreachable: {exc}")
    yield ops
    await ops.close()


@pytest.fixture
def redis(redis_ops: RedisOps) -> Redis:
    return redis_ops.redis


# ---- service -----------------------------------------------------------------------------------


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def settings(pg_dsn: str, redis_url: str) -> Settings:
    return Settings(
        postgres_dsn=pg_dsn,
        redis_url=redis_url,
        jwt_secret=JWT_SECRET,
        service_token=SERVICE_TOKEN,
        session_id=f"ses_test{secrets.token_hex(6)}",
    )


@pytest.fixture
async def service(
    engine: AsyncEngine, redis_ops: RedisOps, settings: Settings, clock: Clock
) -> ChatService:
    svc = ChatService(engine, redis_ops, settings, clock=clock)
    await svc.bootstrap()
    return svc


@pytest.fixture
async def gateways(redis: Redis) -> AsyncIterator[FakeGateways]:
    fake = FakeGateways(redis)
    yield fake
    await fake.close()
