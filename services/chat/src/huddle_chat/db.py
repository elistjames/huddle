"""Engine creation, schema migration and persistence-failure classification."""

from __future__ import annotations

from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from asyncpg import exceptions as asyncpg_exc
from sqlalchemy import exc as sa_exc
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from huddle_chat.config import to_async_dsn

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
# Arbitrary constant shared by every chat replica: serialises migrations and bootstrap.
ADVISORY_LOCK_KEY = 0x48554444  # "HUDD"


def create_engine(dsn: str, *, pool_size: int = 10) -> AsyncEngine:
    return create_async_engine(
        to_async_dsn(dsn),
        pool_size=pool_size,
        max_overflow=10,
        pool_pre_ping=True,
        pool_timeout=5,
        # Fail fast when Postgres is unreachable: sends are rejected, never buffered (D-008).
        connect_args={"timeout": 5, "command_timeout": 15},
    )


def alembic_config() -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    return cfg


def _upgrade(connection: Connection, revision: str) -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = connection
    command.upgrade(cfg, revision)


async def migrate(engine: AsyncEngine, revision: str = "head") -> None:
    """Upgrade the schema in one transaction, serialised across replicas by an advisory lock."""
    async with engine.begin() as conn:
        await conn.execute(sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY})
        await conn.run_sync(_upgrade, revision)


_CONNECTION_ERRORS = (
    asyncpg_exc.PostgresConnectionError,
    asyncpg_exc.ConnectionDoesNotExistError,
    asyncpg_exc.CannotConnectNowError,
    asyncpg_exc.InterfaceError,
    asyncpg_exc.OperatorInterventionError,  # admin shutdown, crash shutdown, cannot connect now
    ConnectionError,
    OSError,
    TimeoutError,
)


def is_persistence_failure(exc: BaseException) -> bool:
    """True when Postgres is unreachable or the connection broke, as opposed to a bug/constraint."""
    if isinstance(exc, sa_exc.DBAPIError):
        if exc.connection_invalidated:
            return True
        if isinstance(exc, sa_exc.OperationalError | sa_exc.InterfaceError):
            return True
        orig = exc.orig
        return orig is not None and isinstance(orig, _CONNECTION_ERRORS)
    if isinstance(exc, sa_exc.TimeoutError):  # pool checkout timed out
        return True
    return isinstance(exc, _CONNECTION_ERRORS)
