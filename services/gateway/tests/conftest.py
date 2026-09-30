from __future__ import annotations

import os
from collections.abc import AsyncIterator

import pytest
from redis.asyncio import Redis
from redis.exceptions import RedisError


def _unavailable(reason: str) -> None:
    # CI sets HUDDLE_REQUIRE_STORES=1 so a missing store fails instead of silently skipping.
    if os.environ.get("HUDDLE_REQUIRE_STORES") == "1":
        pytest.fail(reason)
    pytest.skip(reason)


@pytest.fixture
async def redis_url() -> str:
    url = os.environ.get("HUDDLE_TEST_REDIS_URL")
    if not url:
        _unavailable('HUDDLE_TEST_REDIS_URL not set (eval "$(scripts/dev-stores.sh)")')
        raise AssertionError("unreachable")
    client: Redis = Redis.from_url(url)  # pyright: ignore[reportUnknownMemberType]
    try:
        await client.ping()  # pyright: ignore[reportUnknownMemberType]
    except (RedisError, OSError) as exc:
        _unavailable(f"Redis at {url} unreachable: {exc}")
    finally:
        await client.aclose()
    return url


@pytest.fixture
async def redis(redis_url: str) -> AsyncIterator[Redis]:
    """A client for inspecting registry state. Tests use unique ids and never flush: the
    local Redis may be shared with other test runs."""
    client: Redis = Redis.from_url(redis_url, decode_responses=True)  # pyright: ignore[reportUnknownMemberType]
    try:
        yield client
    finally:
        await client.aclose()
