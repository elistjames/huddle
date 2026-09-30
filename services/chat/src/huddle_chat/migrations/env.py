"""Alembic environment for the chat service (the owner of every Huddle migration).

Two ways in:
- programmatically from `huddle_chat.migrate`, which passes an open connection (already holding
  the migration advisory lock) in `config.attributes["connection"]`;
- from the CLI: `uv run alembic -c services/chat/alembic.ini upgrade head`, which reads
  HUDDLE_POSTGRES_DSN.

Migrations follow expand-then-contract (Deep Dive §4, §12): a release only adds (tables, nullable
columns, indexes built concurrently where large); removals ship in a later release once no running
version reads the old shape.
"""

from __future__ import annotations

import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from huddle_chat.config import to_async_dsn
from huddle_chat.schema import metadata

config = context.config


def _url() -> str:
    return config.get_main_option("sqlalchemy.url") or to_async_dsn(
        os.environ.get("HUDDLE_POSTGRES_DSN", "")
    )


def _run(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def _run_async() -> None:
    engine = create_async_engine(_url(), poolclass=NullPool)
    try:
        async with engine.connect() as conn:
            await conn.run_sync(_run)
            await conn.commit()
    finally:
        await engine.dispose()


if context.is_offline_mode():
    context.configure(url=_url(), target_metadata=metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    given: Connection | None = config.attributes.get("connection")
    if given is not None:
        _run(given)
    else:
        asyncio.run(_run_async())
