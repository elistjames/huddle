"""Migrations match the Core schema, and the §4 constraints hold in a real database."""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine

from huddle_chat.db import alembic_config, migrate
from huddle_chat.schema import agent_runs, metadata
from huddle_chat.service import ChatService

pytestmark = pytest.mark.integration


def _diff(conn: Connection) -> list[Any]:
    ctx = MigrationContext.configure(conn, opts={"compare_type": True})
    return list(compare_metadata(ctx, metadata))


async def test_migrated_schema_matches_core_tables(engine: AsyncEngine) -> None:
    async with engine.connect() as conn:
        diff = await conn.run_sync(_diff)
    assert diff == []


def _downgrade_to_base(conn: Connection) -> None:
    cfg = alembic_config()
    cfg.attributes["connection"] = conn
    command.downgrade(cfg, "base")


async def test_downgrade_then_upgrade_roundtrip_and_idempotent_migrate(
    engine: AsyncEngine,
) -> None:
    async with engine.begin() as conn:
        await conn.run_sync(_downgrade_to_base)
        tables = (
            await conn.execute(
                sa.text("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
            )
        ).scalar()
    assert tables == 1  # only alembic_version is left
    await migrate(engine)
    await migrate(engine)
    async with engine.connect() as conn:
        version = (await conn.execute(sa.text("SELECT version_num FROM alembic_version"))).scalar()
    assert version == "0001"


async def test_only_one_live_run_per_session(service: ChatService) -> None:
    sid = service.settings.session_id
    async with service.tx() as conn:
        agent_id = (
            await conn.execute(sa.text("SELECT id FROM agents ORDER BY created_at LIMIT 1"))
        ).scalar_one()

    def run(status: str) -> dict[str, object]:
        return {
            "id": str(uuid.uuid4()),
            "session_id": sid,
            "agent_id": agent_id,
            "trigger_kind": "message",
            "trigger_seq": 1,
            "context_seq": 1,
            "status": status,
        }

    async with service.engine.begin() as conn:
        await conn.execute(sa.insert(agent_runs), [run("committed"), run("observed")])
        await conn.execute(sa.insert(agent_runs), [run("generating")])
    with pytest.raises(IntegrityError):
        async with service.engine.begin() as conn:
            await conn.execute(sa.insert(agent_runs), [run("eligible")])
