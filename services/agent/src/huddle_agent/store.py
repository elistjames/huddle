"""Postgres access for the agent (Deep Dive §4). The chat service owns every migration.

Reads: `chat_sessions`, `session_memberships`, `agents`, `messages`.
Writes: `agent_runs`, `session_summaries` (docs/work/README.md).

The table shapes below list only the columns this service touches. Optional columns (for
example `agent_runs.prompt_version`, required by Deep Dive §4/§12 but absent from the §4 field
list) are probed at startup and written only when the chat service's migration created them.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import RowMapping
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine

from huddle_agent.context import message_tokens
from huddle_agent.domain import (
    LIVE_STATUSES,
    AgentInfo,
    ChatMessage,
    GateSnapshot,
    RunRecord,
    RunStatus,
    SessionInfo,
    Summary,
    TriggerKind,
)
from huddle_contracts.models import MessageKind, SenderType

# Runs whose context_seq counts as "the agent already evaluated up to here" (dedupes
# at-least-once redeliveries without suppressing re-checks after time-based gate skips).
EVALUATED_STATUSES = (RunStatus.OBSERVED.value, RunStatus.COMMITTED.value)
EVALUATED_SKIPS = ("decider_declined",)
# Nudge skips that do not consume the lull's single nudge attempt.
NUDGE_NOT_ATTEMPTED = ("lull_not_over",)

chat_sessions = sa.table(
    "chat_sessions",
    sa.column("id", sa.Text),
    sa.column("status", sa.Text),
    sa.column("last_seq", sa.BigInteger),
    sa.column("last_human_message_at", sa.DateTime(timezone=True)),
)
session_memberships = sa.table(
    "session_memberships",
    sa.column("session_id", sa.Text),
    sa.column("participant_id", sa.Text),
    sa.column("participant_type", sa.Text),
    sa.column("status", sa.Text),
)
messages = sa.table(
    "messages",
    sa.column("id", sa.Text),
    sa.column("session_id", sa.Text),
    sa.column("seq", sa.BigInteger),
    sa.column("sender_type", sa.Text),
    sa.column("sender_id", sa.Text),
    sa.column("client_msg_id", sa.Text),
    sa.column("kind", sa.Text),
    sa.column("body", sa.Text),
    sa.column("created_at", sa.DateTime(timezone=True)),
)
agents = sa.table(
    "agents",
    sa.column("id", sa.Text),
    sa.column("name", sa.Text),
    sa.column("model", sa.Text),
    sa.column("fallback_model", sa.Text),
    sa.column("prompt_version", sa.Text),
    sa.column("config", JSONB),
    sa.column("status", sa.Text),
)
agent_runs = sa.table(
    "agent_runs",
    sa.column("id", sa.Text),
    sa.column("session_id", sa.Text),
    sa.column("agent_id", sa.Text),
    sa.column("trigger_kind", sa.Text),
    sa.column("trigger_seq", sa.BigInteger),
    sa.column("context_seq", sa.BigInteger),
    sa.column("status", sa.Text),
    sa.column("skip_reason", sa.Text),
    sa.column("error_code", sa.Text),
    sa.column("lease_owner", sa.Text),
    sa.column("lease_expires_at", sa.DateTime(timezone=True)),
    sa.column("attempts", sa.Integer),
    sa.column("model", sa.Text),
    sa.column("input_tokens", sa.Integer),
    sa.column("output_tokens", sa.Integer),
    sa.column("latency_ms", sa.Integer),
    sa.column("output_message_id", sa.Text),
    sa.column("prompt_version", sa.Text),
    sa.column("created_at", sa.DateTime(timezone=True)),
    sa.column("updated_at", sa.DateTime(timezone=True)),
)
# `id` (an identity column) and `created_at` have server defaults; the agent never sets them.
session_summaries = sa.table(
    "session_summaries",
    sa.column("session_id", sa.Text),
    sa.column("agent_id", sa.Text),
    sa.column("covers_through_seq", sa.BigInteger),
    sa.column("summary", sa.Text),
    sa.column("token_count", sa.Integer),
    sa.column("model", sa.Text),
    sa.column("created_at", sa.DateTime(timezone=True)),
)

_RUN_FIELDS = (
    "status",
    "context_seq",
    "skip_reason",
    "error_code",
    "model",
    "input_tokens",
    "output_tokens",
    "latency_ms",
    "output_message_id",
)


@dataclass(frozen=True)
class RearmCandidate:
    session_id: str
    agent: AgentInfo
    last_human_message_at: datetime
    nudge_done: bool


class Store(Protocol):
    async def load_gate_snapshot(
        self, session_id: str, share_window: int
    ) -> GateSnapshot | None: ...

    async def load_window(
        self, session_id: str, agent_id: str, max_tokens: int
    ) -> tuple[Summary | None, list[ChatMessage]]: ...

    async def latest_human_seq(self, session_id: str) -> int: ...

    async def latest_seq(self, session_id: str) -> int: ...

    async def insert_summary(self, session_id: str, agent_id: str, summary: Summary) -> None: ...

    async def find_live_run(self, session_id: str, agent_id: str) -> RunRecord | None: ...

    async def create_run(self, run: RunRecord, prompt_version: str | None) -> None: ...

    async def promote_to_eligible(self, run_id: str, owner: str) -> bool: ...

    async def takeover_run(
        self, run_id: str, owner: str, now: datetime, lease_until: datetime
    ) -> RunRecord | None: ...

    async def update_run(self, run_id: str, owner: str, **fields: Any) -> bool: ...

    async def renew_lease(self, run_id: str, owner: str, lease_until: datetime) -> bool: ...

    async def expire_orphans(self, now: datetime) -> int: ...

    async def agent_message_for_run(
        self, session_id: str, agent_id: str, run_id: str
    ) -> tuple[str, int] | None: ...

    async def rearm_candidates(self) -> list[RearmCandidate]: ...

    async def get_run(self, run_id: str) -> RunRecord | None: ...


def asyncpg_url(dsn: str) -> str:
    for prefix in ("postgresql+asyncpg://", "postgresql://", "postgres://"):
        if dsn.startswith(prefix):
            return "postgresql+asyncpg://" + dsn[len(prefix) :]
    return dsn


def _msg(row: RowMapping) -> ChatMessage:
    return ChatMessage(
        id=row["id"],
        seq=int(row["seq"]),
        sender_type=SenderType(row["sender_type"]),
        sender_id=row["sender_id"],
        kind=MessageKind(row["kind"]),
        body=row["body"],
        created_at=row["created_at"],
        client_msg_id=str(row["client_msg_id"]),
    )


def _run(row: RowMapping) -> RunRecord:
    return RunRecord(
        id=row["id"],
        session_id=row["session_id"],
        agent_id=row["agent_id"],
        trigger_kind=TriggerKind(row["trigger_kind"]),
        trigger_seq=int(row["trigger_seq"]),
        context_seq=int(row["context_seq"]),
        status=RunStatus(row["status"]),
        attempts=int(row["attempts"] or 1),
        skip_reason=row["skip_reason"],
        error_code=row["error_code"],
        lease_owner=row["lease_owner"],
        lease_expires_at=row["lease_expires_at"],
        model=row["model"],
        input_tokens=int(row["input_tokens"] or 0),
        output_tokens=int(row["output_tokens"] or 0),
        latency_ms=row["latency_ms"],
        output_message_id=row["output_message_id"],
    )


_RUN_COLS = [
    agent_runs.c[name]
    for name in (
        "id",
        "session_id",
        "agent_id",
        "trigger_kind",
        "trigger_seq",
        "context_seq",
        "status",
        "attempts",
        "skip_reason",
        "error_code",
        "lease_owner",
        "lease_expires_at",
        "model",
        "input_tokens",
        "output_tokens",
        "latency_ms",
        "output_message_id",
    )
]
_LIVE = [s.value for s in LIVE_STATUSES]
# A run's row may change only while it is not terminal (and only by its lease owner).
_MUTABLE = [RunStatus.TRIGGERED.value, *_LIVE, RunStatus.COMMITTED.value]


class PostgresStore:
    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine
        self._columns: dict[str, set[str]] = {}

    @classmethod
    def from_dsn(cls, dsn: str) -> PostgresStore:
        return cls(create_async_engine(asyncpg_url(dsn), pool_pre_ping=True, pool_size=5))

    async def dispose(self) -> None:
        await self._engine.dispose()

    async def ping(self) -> bool:
        try:
            async with self._engine.connect() as conn:
                await conn.execute(sa.select(1))
            return True
        except Exception:  # health probe only; the reason is irrelevant
            return False

    async def probe_columns(self) -> None:
        """Learn which optional columns the chat service's migrations created."""
        q = sa.text(
            "SELECT table_name, column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name IN "
            "('agent_runs', 'session_summaries')"
        )
        async with self._engine.connect() as conn:
            rows = (await conn.execute(q)).all()
        cols: dict[str, set[str]] = {}
        for table_name, column_name in rows:
            cols.setdefault(str(table_name), set()).add(str(column_name))
        self._columns = cols

    def _filter(self, table: str, values: dict[str, Any]) -> dict[str, Any]:
        known = self._columns.get(table)
        if not known:
            return values
        return {k: v for k, v in values.items() if k in known}

    def _has(self, table: str, column: str) -> bool:
        return column in self._columns.get(table, set())

    # ---- reads ----------------------------------------------------------------------------

    async def _agent_for_session(self, conn: AsyncConnection, session_id: str) -> AgentInfo | None:
        q = (
            sa.select(
                agents.c.id,
                agents.c.name,
                agents.c.status,
                agents.c.model,
                agents.c.fallback_model,
                agents.c.prompt_version,
                agents.c.config,
            )
            .select_from(
                session_memberships.join(
                    agents, agents.c.id == session_memberships.c.participant_id
                )
            )
            .where(
                session_memberships.c.session_id == session_id,
                session_memberships.c.participant_type == "agent",
                session_memberships.c.status == "active",
            )
            .limit(1)
        )
        row = (await conn.execute(q)).mappings().first()
        if row is None:
            return None
        return AgentInfo(
            id=row["id"],
            name=row["name"],
            status=row["status"],
            model=row["model"] or "",
            fallback_model=row["fallback_model"] or None,
            prompt_version=row["prompt_version"] or "",
            config=dict(row["config"] or {}),
        )

    async def load_gate_snapshot(self, session_id: str, share_window: int) -> GateSnapshot | None:
        async with self._engine.connect() as conn:
            srow = (
                (
                    await conn.execute(
                        sa.select(
                            chat_sessions.c.id,
                            chat_sessions.c.status,
                            chat_sessions.c.last_seq,
                            chat_sessions.c.last_human_message_at,
                        ).where(chat_sessions.c.id == session_id)
                    )
                )
                .mappings()
                .first()
            )
            if srow is None:
                return None
            agent = await self._agent_for_session(conn, session_id)
            if agent is None:
                return None
            session = SessionInfo(
                id=srow["id"],
                status=srow["status"],
                last_seq=int(srow["last_seq"]),
                last_human_message_at=srow["last_human_message_at"],
            )
            recent_rows = (
                (
                    await conn.execute(
                        sa.select(messages)
                        .where(messages.c.session_id == session_id, messages.c.kind == "text")
                        .order_by(messages.c.seq.desc())
                        .limit(share_window)
                    )
                )
                .mappings()
                .all()
            )
            recent = [_msg(r) for r in reversed(recent_rows)]
            last_agent_row = (
                (
                    await conn.execute(
                        sa.select(messages)
                        .where(
                            messages.c.session_id == session_id,
                            messages.c.sender_type == "agent",
                            messages.c.sender_id == agent.id,
                            messages.c.kind == "text",
                        )
                        .order_by(messages.c.seq.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            n = (
                await conn.execute(
                    sa.select(sa.func.count())
                    .select_from(session_memberships)
                    .where(
                        session_memberships.c.session_id == session_id,
                        session_memberships.c.status == "active",
                    )
                )
            ).scalar_one()
            evaluated = (
                await conn.execute(
                    sa.select(sa.func.coalesce(sa.func.max(agent_runs.c.context_seq), 0)).where(
                        agent_runs.c.session_id == session_id,
                        agent_runs.c.agent_id == agent.id,
                        agent_runs.c.trigger_kind == TriggerKind.MESSAGE.value,
                        sa.or_(
                            agent_runs.c.status.in_(EVALUATED_STATUSES),
                            sa.and_(
                                agent_runs.c.status == RunStatus.SKIPPED.value,
                                agent_runs.c.skip_reason.in_(EVALUATED_SKIPS),
                            ),
                        ),
                    )
                )
            ).scalar_one()
            nudge_done = await self._nudge_done(conn, session_id, agent.id)
        return GateSnapshot(
            session=session,
            agent=agent,
            recent=recent,
            active_participants=int(n),
            last_agent_message=_msg(last_agent_row) if last_agent_row else None,
            last_evaluated_seq=int(evaluated),
            nudge_done_for_lull=nudge_done,
        )

    async def _last_human_seq(self, conn: AsyncConnection, session_id: str) -> int:
        seq = (
            await conn.execute(
                sa.select(messages.c.seq)
                .where(
                    messages.c.session_id == session_id,
                    messages.c.sender_type == "user",
                    messages.c.kind == "text",
                )
                .order_by(messages.c.seq.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        return int(seq or 0)

    async def _nudge_done(self, conn: AsyncConnection, session_id: str, agent_id: str) -> bool:
        """A lull is identified by the seq of the last human message before it; a nudge run
        records that seq as its trigger_seq."""
        lull_seq = await self._last_human_seq(conn, session_id)
        found = (
            await conn.execute(
                sa.select(agent_runs.c.id)
                .where(
                    agent_runs.c.session_id == session_id,
                    agent_runs.c.agent_id == agent_id,
                    agent_runs.c.trigger_kind == TriggerKind.NUDGE.value,
                    agent_runs.c.trigger_seq >= lull_seq,
                    sa.or_(
                        agent_runs.c.skip_reason.is_(None),
                        agent_runs.c.skip_reason.not_in(NUDGE_NOT_ATTEMPTED),
                    ),
                )
                .limit(1)
            )
        ).first()
        return found is not None

    async def load_window(
        self, session_id: str, agent_id: str, max_tokens: int
    ) -> tuple[Summary | None, list[ChatMessage]]:
        """Latest summary plus the newest messages after it, up to ~max_tokens. Pages backwards
        in batches of 200 and stops at the cap: never the full history (Deep Dive §5)."""
        async with self._engine.connect() as conn:
            srow = (
                (
                    await conn.execute(
                        sa.select(session_summaries)
                        .where(
                            session_summaries.c.session_id == session_id,
                            session_summaries.c.agent_id == agent_id,
                        )
                        .order_by(session_summaries.c.covers_through_seq.desc())
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
            summary = (
                Summary(
                    covers_through_seq=int(srow["covers_through_seq"]),
                    summary=srow["summary"],
                    token_count=int(srow["token_count"] or 0),
                    model=srow["model"] or "",
                )
                if srow
                else None
            )
            floor = summary.covers_through_seq if summary else 0
            out: list[ChatMessage] = []
            acc = 0
            before: int | None = None
            while acc <= max_tokens:
                q = sa.select(messages).where(
                    messages.c.session_id == session_id, messages.c.seq > floor
                )
                if before is not None:
                    q = q.where(messages.c.seq < before)
                rows = (
                    (await conn.execute(q.order_by(messages.c.seq.desc()).limit(200)))
                    .mappings()
                    .all()
                )
                if not rows:
                    break
                for r in rows:
                    m = _msg(r)
                    out.append(m)
                    acc += message_tokens(m)
                    if acc > max_tokens:
                        break
                before = int(rows[-1]["seq"])
        out.reverse()
        return summary, out

    async def latest_human_seq(self, session_id: str) -> int:
        async with self._engine.connect() as conn:
            return await self._last_human_seq(conn, session_id)

    async def latest_seq(self, session_id: str) -> int:
        async with self._engine.connect() as conn:
            seq = (
                await conn.execute(
                    sa.select(chat_sessions.c.last_seq).where(chat_sessions.c.id == session_id)
                )
            ).scalar_one_or_none()
        return int(seq or 0)

    async def insert_summary(self, session_id: str, agent_id: str, summary: Summary) -> None:
        values: dict[str, Any] = {
            "session_id": session_id,
            "agent_id": agent_id,
            "covers_through_seq": summary.covers_through_seq,
            "summary": summary.summary,
            "token_count": summary.token_count,
            "model": summary.model,
        }
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(session_summaries).values(**values))

    # ---- runs -----------------------------------------------------------------------------

    async def get_run(self, run_id: str) -> RunRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (await conn.execute(sa.select(*_RUN_COLS).where(agent_runs.c.id == run_id)))
                .mappings()
                .first()
            )
        return _run(row) if row else None

    async def find_live_run(self, session_id: str, agent_id: str) -> RunRecord | None:
        async with self._engine.connect() as conn:
            row = (
                (
                    await conn.execute(
                        sa.select(*_RUN_COLS)
                        .where(
                            agent_runs.c.session_id == session_id,
                            agent_runs.c.agent_id == agent_id,
                            agent_runs.c.status.in_(_LIVE),
                        )
                        .limit(1)
                    )
                )
                .mappings()
                .first()
            )
        return _run(row) if row else None

    async def create_run(self, run: RunRecord, prompt_version: str | None) -> None:
        values: dict[str, Any] = {
            "id": run.id,
            "session_id": run.session_id,
            "agent_id": run.agent_id,
            "trigger_kind": run.trigger_kind.value,
            "trigger_seq": run.trigger_seq,
            "context_seq": run.context_seq,
            "status": run.status.value,
            "attempts": run.attempts,
            "lease_owner": run.lease_owner,
            "lease_expires_at": run.lease_expires_at,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        if prompt_version is not None and self._has("agent_runs", "prompt_version"):
            values["prompt_version"] = prompt_version
        for ts in ("created_at", "updated_at"):
            if self._has("agent_runs", ts):
                values[ts] = sa.func.now()
        async with self._engine.begin() as conn:
            await conn.execute(sa.insert(agent_runs).values(**self._filter("agent_runs", values)))

    def _touch(self, values: dict[str, Any]) -> dict[str, Any]:
        if self._has("agent_runs", "updated_at"):
            values["updated_at"] = sa.func.now()
        return values

    async def promote_to_eligible(self, run_id: str, owner: str) -> bool:
        """triggered -> eligible. The partial unique index rejects a second live run."""
        try:
            async with self._engine.begin() as conn:
                res = await conn.execute(
                    sa.update(agent_runs)
                    .where(
                        agent_runs.c.id == run_id,
                        agent_runs.c.lease_owner == owner,
                        agent_runs.c.status == RunStatus.TRIGGERED.value,
                    )
                    .values(**self._touch({"status": RunStatus.ELIGIBLE.value}))
                )
                return res.rowcount == 1
        except IntegrityError:
            return False

    async def takeover_run(
        self, run_id: str, owner: str, now: datetime, lease_until: datetime
    ) -> RunRecord | None:
        async with self._engine.begin() as conn:
            row = (
                (
                    await conn.execute(
                        sa.update(agent_runs)
                        .where(
                            agent_runs.c.id == run_id,
                            agent_runs.c.status.in_(_LIVE),
                            sa.or_(
                                agent_runs.c.lease_expires_at.is_(None),
                                agent_runs.c.lease_expires_at < now,
                            ),
                        )
                        .values(
                            **self._touch(
                                {
                                    "lease_owner": owner,
                                    "lease_expires_at": lease_until,
                                    "attempts": agent_runs.c.attempts + 1,
                                    "status": RunStatus.ELIGIBLE.value,
                                }
                            )
                        )
                        .returning(*_RUN_COLS)
                    )
                )
                .mappings()
                .first()
            )
        return _run(row) if row else None

    async def update_run(self, run_id: str, owner: str, **fields: Any) -> bool:
        unknown = set(fields) - set(_RUN_FIELDS)
        if unknown:
            raise ValueError(f"unknown run fields: {sorted(unknown)}")
        values = {k: (v.value if isinstance(v, RunStatus) else v) for k, v in fields.items()}
        async with self._engine.begin() as conn:
            res = await conn.execute(
                sa.update(agent_runs)
                .where(
                    agent_runs.c.id == run_id,
                    agent_runs.c.lease_owner == owner,
                    agent_runs.c.status.in_(_MUTABLE),
                )
                .values(**self._touch(values))
            )
            return res.rowcount == 1

    async def renew_lease(self, run_id: str, owner: str, lease_until: datetime) -> bool:
        async with self._engine.begin() as conn:
            res = await conn.execute(
                sa.update(agent_runs)
                .where(
                    agent_runs.c.id == run_id,
                    agent_runs.c.lease_owner == owner,
                    agent_runs.c.status.in_(_MUTABLE),
                )
                .values(lease_expires_at=lease_until)
            )
            return res.rowcount == 1

    async def expire_orphans(self, now: datetime) -> int:
        """Runs stuck in `triggered` (gate stage) whose worker died: mark them timed out.
        Live runs are not touched here; they are taken over and rerun instead."""
        async with self._engine.begin() as conn:
            res = await conn.execute(
                sa.update(agent_runs)
                .where(
                    agent_runs.c.status == RunStatus.TRIGGERED.value,
                    agent_runs.c.lease_expires_at < now,
                )
                .values(
                    **self._touch(
                        {"status": RunStatus.TIMED_OUT.value, "error_code": "lease_expired"}
                    )
                )
            )
            return int(res.rowcount)

    async def agent_message_for_run(
        self, session_id: str, agent_id: str, run_id: str
    ) -> tuple[str, int] | None:
        async with self._engine.connect() as conn:
            row = (
                await conn.execute(
                    sa.select(messages.c.id, messages.c.seq).where(
                        messages.c.session_id == session_id,
                        messages.c.sender_id == agent_id,
                        sa.cast(messages.c.client_msg_id, sa.Text) == run_id,
                    )
                )
            ).first()
        return (str(row[0]), int(row[1])) if row else None

    async def rearm_candidates(self) -> list[RearmCandidate]:
        async with self._engine.connect() as conn:
            rows = (
                await conn.execute(
                    sa.select(chat_sessions.c.id, chat_sessions.c.last_human_message_at).where(
                        chat_sessions.c.status == "active",
                        chat_sessions.c.last_human_message_at.is_not(None),
                    )
                )
            ).all()
            out: list[RearmCandidate] = []
            for session_id, last_human in rows:
                agent = await self._agent_for_session(conn, str(session_id))
                if agent is None:
                    continue
                done = await self._nudge_done(conn, str(session_id), agent.id)
                out.append(RearmCandidate(str(session_id), agent, last_human, done))
        return out
