"""Postgres access (SQLAlchemy Core). Every function runs on a caller-owned connection so the
service decides the transaction boundaries (Deep Dive §7: membership check, seq increment and
insert in one transaction; fan-out and triggers after commit)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection

from huddle_chat.ids import new_id
from huddle_chat.schema import (
    agent_budgets,
    agent_runs,
    agents,
    audit_events,
    chat_sessions,
    messages,
    refresh_tokens,
    session_memberships,
    users,
)
from huddle_contracts import budget as budget_math
from huddle_contracts.models import MemberView, Message, MessageKind, ParticipantType, SenderType

SYSTEM_SENDER_ID = "system"


@dataclass(frozen=True)
class Membership:
    session_id: str
    participant_id: str
    participant_type: str
    role: str
    status: str
    last_delivered_seq: int
    last_read_seq: int

    @property
    def is_active(self) -> bool:
        return self.status == "active"


@dataclass(frozen=True)
class AgentRow:
    id: str
    name: str
    status: str  # active | paused | removed


@dataclass(frozen=True)
class BudgetRow:
    session_id: str
    agent_id: str
    capacity_tokens: int
    used_tokens: int
    window_seconds: int
    window_started_at: datetime
    prior_used_tokens: int = 0  # as stored before this call's window roll and debit

    @property
    def prior_visible(self) -> tuple[int, bool]:
        """(used_pct, resting) as users last saw it, before this call changed anything."""
        return (
            budget_math.used_pct(self.prior_used_tokens, self.capacity_tokens),
            budget_math.is_resting(self.prior_used_tokens, self.capacity_tokens),
        )

    @property
    def refills_at(self) -> datetime:
        return budget_math.window_end(self.window_started_at, self.window_seconds)

    @property
    def used_pct(self) -> int:
        return budget_math.used_pct(self.used_tokens, self.capacity_tokens)

    @property
    def resting(self) -> bool:
        return budget_math.is_resting(self.used_tokens, self.capacity_tokens)


def to_message(row: Row[Any]) -> Message:
    return Message(
        id=row.id,
        session_id=row.session_id,
        seq=row.seq,
        sender_type=SenderType(row.sender_type),
        sender_id=row.sender_id,
        client_msg_id=str(row.client_msg_id),
        kind=MessageKind(row.kind),
        body=row.body,
        created_at=row.created_at,
    )


def canonical_uuid(value: str) -> str | None:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError, TypeError):
        return None


def _membership(row: Row[Any] | None) -> Membership | None:
    if row is None:
        return None
    return Membership(
        session_id=row.session_id,
        participant_id=row.participant_id,
        participant_type=row.participant_type,
        role=row.role,
        status=row.status,
        last_delivered_seq=row.last_delivered_seq,
        last_read_seq=row.last_read_seq,
    )


# ---- sessions and memberships -----------------------------------------------------------------


async def get_session(conn: AsyncConnection, session_id: str) -> Row[Any] | None:
    q = sa.select(chat_sessions).where(chat_sessions.c.id == session_id)
    return (await conn.execute(q)).first()


async def lock_session(conn: AsyncConnection, session_id: str) -> Row[Any] | None:
    """Row-lock the session: serialises every writer of this session's `seq` (Deep Dive §2)."""
    q = sa.select(chat_sessions).where(chat_sessions.c.id == session_id).with_for_update()
    return (await conn.execute(q)).first()


async def get_membership(
    conn: AsyncConnection, session_id: str, participant_id: str, *, lock: str | None = None
) -> Membership | None:
    """`lock="share"` holds the row against a concurrent removal until the transaction ends;
    `lock="update"` is for cursor writes."""
    q = sa.select(session_memberships).where(
        session_memberships.c.session_id == session_id,
        session_memberships.c.participant_id == participant_id,
    )
    if lock == "share":
        q = q.with_for_update(read=True)
    elif lock == "update":
        q = q.with_for_update()
    return _membership((await conn.execute(q)).first())


async def add_membership(
    conn: AsyncConnection, session_id: str, participant_id: str, participant_type: ParticipantType
) -> None:
    stmt = (
        pg_insert(session_memberships)
        .values(
            session_id=session_id,
            participant_id=participant_id,
            participant_type=participant_type.value,
            role="member",
            status="active",
        )
        .on_conflict_do_nothing()
    )
    await conn.execute(stmt)


async def active_members(conn: AsyncConnection, session_id: str) -> list[tuple[str, str]]:
    """(participant_id, participant_type) for every active member."""
    q = sa.select(
        session_memberships.c.participant_id, session_memberships.c.participant_type
    ).where(
        session_memberships.c.session_id == session_id, session_memberships.c.status == "active"
    )
    return [(r.participant_id, r.participant_type) for r in (await conn.execute(q)).all()]


async def active_user_ids(conn: AsyncConnection, session_id: str) -> list[str]:
    return [pid for pid, ptype in await active_members(conn, session_id) if ptype == "user"]


async def cursors(conn: AsyncConnection, session_id: str) -> dict[str, int]:
    q = sa.select(
        session_memberships.c.participant_id, session_memberships.c.last_delivered_seq
    ).where(
        session_memberships.c.session_id == session_id, session_memberships.c.status == "active"
    )
    return {r.participant_id: r.last_delivered_seq for r in (await conn.execute(q)).all()}


async def advance_cursors(
    conn: AsyncConnection, session_id: str, participant_id: str, delivered: int, read: int
) -> None:
    """Cursors only move forward (Deep Dive §2, §7): GREATEST guards against a stale ack."""
    m = session_memberships.c
    await conn.execute(
        sa.update(session_memberships)
        .where(m.session_id == session_id, m.participant_id == participant_id)
        .values(
            last_delivered_seq=sa.func.greatest(m.last_delivered_seq, delivered),
            last_read_seq=sa.func.greatest(m.last_read_seq, read),
        )
    )


async def members_view(conn: AsyncConnection, session_id: str) -> list[MemberView]:
    m = session_memberships.c
    name = sa.func.coalesce(users.c.display_name, agents.c.name, m.participant_id)
    q = (
        sa.select(m.participant_id, m.participant_type, m.role, m.status, name.label("name"))
        .select_from(
            session_memberships.outerjoin(users, users.c.id == m.participant_id).outerjoin(
                agents, agents.c.id == m.participant_id
            )
        )
        .where(m.session_id == session_id)
        .order_by(m.joined_at, m.participant_id)
    )
    return [
        MemberView(
            participant_id=r.participant_id,
            participant_type=ParticipantType(r.participant_type),
            display_name=r.name,
            role=r.role,
            status=r.status,
        )
        for r in (await conn.execute(q)).all()
    ]


async def display_name(conn: AsyncConnection, participant_id: str) -> str | None:
    q = sa.union_all(
        sa.select(users.c.display_name.label("name")).where(users.c.id == participant_id),
        sa.select(agents.c.name.label("name")).where(agents.c.id == participant_id),
    )
    value: Any = (await conn.execute(q)).scalar()
    return None if value is None else str(value)


# ---- messages ----------------------------------------------------------------------------------


async def find_message(
    conn: AsyncConnection, session_id: str, sender_id: str, client_msg_id: str
) -> Row[Any] | None:
    q = sa.select(messages).where(
        messages.c.session_id == session_id,
        messages.c.sender_id == sender_id,
        messages.c.client_msg_id == client_msg_id,
    )
    return (await conn.execute(q)).first()


async def next_seq(
    conn: AsyncConnection, session_id: str, *, human_at: datetime | None = None
) -> int:
    """Gapless per-session seq (D-001): the increment rolls back with the transaction."""
    values: dict[str, Any] = {"last_seq": chat_sessions.c.last_seq + 1}
    if human_at is not None:
        values["last_human_message_at"] = human_at
    stmt = (
        sa.update(chat_sessions)
        .where(chat_sessions.c.id == session_id)
        .values(**values)
        .returning(chat_sessions.c.last_seq)
    )
    seq: Any = (await conn.execute(stmt)).scalar_one()
    return int(seq)


async def insert_message(
    conn: AsyncConnection,
    *,
    session_id: str,
    seq: int,
    sender_type: SenderType,
    sender_id: str,
    client_msg_id: str,
    kind: MessageKind,
    body: str,
    agent_run_id: str | None = None,
) -> Row[Any]:
    run_ref: Any = None
    if agent_run_id is not None:
        # Link the run only if the agent already recorded it (agent_runs is the agent's table).
        run_ref = (
            sa.select(agent_runs.c.id).where(agent_runs.c.id == agent_run_id).scalar_subquery()
        )
    stmt = (
        sa.insert(messages)
        .values(
            id=new_id("msg"),
            session_id=session_id,
            seq=seq,
            sender_type=sender_type.value,
            sender_id=sender_id,
            client_msg_id=client_msg_id,
            kind=kind.value,
            body=body,
            agent_run_id=run_ref,
        )
        .returning(messages)
    )
    return (await conn.execute(stmt)).one()


async def page_messages(
    conn: AsyncConnection,
    session_id: str,
    *,
    after_seq: int | None,
    before_seq: int | None,
    limit: int,
) -> tuple[list[Message], bool]:
    """after_seq only: oldest-first catch-up; has_more means newer messages exist.
    before_seq (or neither: the latest window): the `limit` messages just before it, returned in
    seq order; has_more means older messages exist."""
    c = messages.c
    q = sa.select(messages).where(c.session_id == session_id)
    if after_seq is not None:
        q = q.where(c.seq > after_seq)
    if before_seq is None and after_seq is not None:
        rows = list((await conn.execute(q.order_by(c.seq).limit(limit + 1))).all())
        return [to_message(r) for r in rows[:limit]], len(rows) > limit
    if before_seq is not None:
        q = q.where(c.seq < before_seq)
    rows = list((await conn.execute(q.order_by(c.seq.desc()).limit(limit + 1))).all())
    page = rows[:limit]
    page.reverse()
    return [to_message(r) for r in page], len(rows) > limit


# ---- users and refresh tokens ------------------------------------------------------------------


async def create_user(conn: AsyncConnection, display_name: str) -> str:
    user_id = new_id("usr")
    await conn.execute(sa.insert(users).values(id=user_id, display_name=display_name))
    return user_id


async def store_refresh_token(
    conn: AsyncConnection, *, user_id: str, family_id: str, token_hash: str, expires_at: datetime
) -> None:
    await conn.execute(
        sa.insert(refresh_tokens).values(
            id=new_id("rt"),
            user_id=user_id,
            family_id=family_id,
            token_hash=token_hash,
            expires_at=expires_at,
        )
    )


async def lock_refresh_token(conn: AsyncConnection, token_hash: str) -> Row[Any] | None:
    q = sa.select(refresh_tokens).where(refresh_tokens.c.token_hash == token_hash).with_for_update()
    return (await conn.execute(q)).first()


async def mark_refresh_used(conn: AsyncConnection, token_id: str, now: datetime) -> None:
    await conn.execute(
        sa.update(refresh_tokens).where(refresh_tokens.c.id == token_id).values(used_at=now)
    )


async def revoke_family(conn: AsyncConnection, family_id: str, now: datetime) -> None:
    await conn.execute(
        sa.update(refresh_tokens)
        .where(refresh_tokens.c.family_id == family_id, refresh_tokens.c.revoked_at.is_(None))
        .values(revoked_at=now)
    )


# ---- agent and budget --------------------------------------------------------------------------


async def session_agent(
    conn: AsyncConnection, session_id: str, *, lock: bool = False
) -> AgentRow | None:
    """The session's agent: an active agent membership whose agent is not removed."""
    m = session_memberships.c
    q = (
        sa.select(agents.c.id, agents.c.name, agents.c.status)
        .select_from(agents.join(session_memberships, m.participant_id == agents.c.id))
        .where(
            m.session_id == session_id,
            m.participant_type == "agent",
            m.status == "active",
            agents.c.status != "removed",
        )
        .order_by(m.joined_at)
        .limit(1)
    )
    if lock:
        q = q.with_for_update(of=agents)
    row = (await conn.execute(q)).first()
    return None if row is None else AgentRow(id=row.id, name=row.name, status=row.status)


async def update_agent(conn: AsyncConnection, agent_id: str, **values: Any) -> None:
    await conn.execute(
        sa.update(agents).where(agents.c.id == agent_id).values(updated_at=sa.func.now(), **values)
    )


async def create_agent(
    conn: AsyncConnection,
    *,
    name: str,
    model: str,
    fallback_model: str,
    prompt_version: str,
    config: dict[str, Any],
) -> str:
    agent_id = new_id("agt")
    await conn.execute(
        sa.insert(agents).values(
            id=agent_id,
            name=name,
            avatar_url="",
            model=model,
            fallback_model=fallback_model,
            prompt_version=prompt_version,
            config=config,
            status="active",
        )
    )
    return agent_id


async def ensure_budget(
    conn: AsyncConnection,
    session_id: str,
    agent_id: str,
    *,
    capacity_tokens: int,
    window_seconds: int,
    now: datetime,
) -> None:
    await conn.execute(
        pg_insert(agent_budgets)
        .values(
            session_id=session_id,
            agent_id=agent_id,
            capacity_tokens=capacity_tokens,
            used_tokens=0,
            window_seconds=window_seconds,
            window_started_at=budget_math.window_start(now, window_seconds),
        )
        .on_conflict_do_nothing()
    )


async def budget(
    conn: AsyncConnection, session_id: str, agent_id: str, now: datetime, *, add_tokens: int = 0
) -> BudgetRow | None:
    """Read (and lock) the budget row, applying the lazy fixed-window reset (D-019) and any debit.

    Writes only when something changed, so plain reads of an unchanged window stay read-only."""
    b = agent_budgets.c
    q = (
        sa.select(agent_budgets)
        .where(b.session_id == session_id, b.agent_id == agent_id)
        .with_for_update()
    )
    row = (await conn.execute(q)).first()
    if row is None:
        return None
    started, used = budget_math.roll(
        now, row.window_started_at, int(row.used_tokens), int(row.window_seconds)
    )
    used += add_tokens
    if started != row.window_started_at or used != row.used_tokens:
        await conn.execute(
            sa.update(agent_budgets)
            .where(b.session_id == session_id, b.agent_id == agent_id)
            .values(used_tokens=used, window_started_at=started, updated_at=sa.func.now())
        )
    return BudgetRow(
        session_id=session_id,
        agent_id=agent_id,
        capacity_tokens=int(row.capacity_tokens),
        used_tokens=used,
        window_seconds=int(row.window_seconds),
        window_started_at=started,
        prior_used_tokens=int(row.used_tokens),
    )


async def audit(
    conn: AsyncConnection,
    *,
    session_id: str | None,
    actor_id: str,
    action: str,
    target: str | None,
    payload: dict[str, Any],
) -> None:
    await conn.execute(
        sa.insert(audit_events).values(
            session_id=session_id, actor_id=actor_id, action=action, target=target, payload=payload
        )
    )
