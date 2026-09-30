"""Chat service core: validation, authorization, sequencing, persistence, fan-out, receipts, budget.

Deep Dive §3 "Authoritative message path": identity -> membership -> length and rate limits ->
sequence-and-insert transaction -> after commit, fan-out and (human messages only) an agent trigger.
"""

from __future__ import annotations

import logging
import time
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa
from pydantic import BaseModel
from redis.exceptions import RedisError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from huddle_chat import auth, store
from huddle_chat.config import Settings
from huddle_chat.db import ADVISORY_LOCK_KEY, is_persistence_failure
from huddle_chat.errors import ChatError
from huddle_chat.ids import new_id
from huddle_chat.obs import TraceContext, current_trace, log_event, trace
from huddle_chat.redis_ops import RedisOps
from huddle_chat.schema import chat_sessions
from huddle_contracts.errors import ErrorCode
from huddle_contracts.internal import (
    AckDeliveryRequest,
    AgentTrigger,
    BudgetState,
    MembershipCheck,
    MessagePage,
    RecordUsageRequest,
    SubmitMessageRequest,
    SubmitMessageResponse,
)
from huddle_contracts.models import (
    AGENT_MAX_BODY_CHARS,
    MAX_BODY_CHARS,
    AgentState,
    AgentView,
    BudgetView,
    Message,
    MessageKind,
    ParticipantType,
    SenderType,
    SessionView,
)
from huddle_contracts.ws import AgentStatus, MemberEvent, MessageCreated, ReceiptUpdated

MAX_PAGE = 200  # Deep Dive §2/§6: replay pages of 200, limit <= 200
MAX_DISPLAY_NAME = 40
ACK_KINDS = ("delivered", "read")

# Deep Dive §5 policy thresholds (tunable); stored in agents.config for the orchestrator.
DEFAULT_AGENT_CONFIG: dict[str, Any] = {
    "debounce_quiet_s": 3,
    "debounce_cap_s": 8,
    "cooldown_s": 20,
    "cooldown_cap_s": 300,
    "share_window_messages": 30,
    "share_cap_factor": 1.25,
    "share_floor_factor": 0.5,
    "max_consecutive_agent_messages": 1,
    "nudge_delay_s": 1200,
    "nudge_delay_min_s": 600,
    "nudge_delay_max_s": 1800,
    "max_chars": AGENT_MAX_BODY_CHARS,
    "decider_timeout_s": 10,
    "writer_timeout_s": 45,
}


def utcnow() -> datetime:
    return datetime.now(UTC)


def effective_state(stored_status: str, reported: AgentState | None, resting: bool) -> AgentState:
    """What users see. `paused` is stored (a person set it); `resting` is derived from the budget
    (D-019); `writing`/`away` are reported by the orchestrator; otherwise it is following along."""
    if stored_status == "paused":
        return AgentState.PAUSED
    if resting:
        return AgentState.RESTING
    if reported == AgentState.WRITING or reported == AgentState.AWAY:
        return reported
    return AgentState.FOLLOWING


def normalize_name(value: str) -> str:
    name = " ".join(value.split())
    if not name or len(name) > MAX_DISPLAY_NAME:
        raise ChatError(ErrorCode.VALIDATION, f"name must be 1 to {MAX_DISPLAY_NAME} characters")
    return name


def _frame(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")


class AckResult(BaseModel):
    last_delivered_seq: int
    last_read_seq: int
    advanced: bool


@dataclass(frozen=True)
class IssuedTokens:
    user_id: str
    access_token: str
    refresh_token: str


class ChatService:
    def __init__(
        self,
        engine: AsyncEngine,
        redis: RedisOps,
        settings: Settings,
        clock: Callable[[], datetime] = utcnow,
    ) -> None:
        self.engine = engine
        self.redis = redis
        self.settings = settings
        self.clock = clock

    # ---- transactions -----------------------------------------------------------------------

    @asynccontextmanager
    async def tx(self) -> AsyncGenerator[AsyncConnection]:
        """One transaction. Postgres being unreachable becomes PERSISTENCE_UNAVAILABLE: the send is
        rejected and never buffered (D-008)."""
        try:
            async with self.engine.begin() as conn:
                yield conn
        except ChatError:
            raise
        except Exception as exc:
            if is_persistence_failure(exc):
                log_event("persistence_unavailable", level=logging.ERROR, error=type(exc).__name__)
                raise ChatError(
                    ErrorCode.PERSISTENCE_UNAVAILABLE, "message store unavailable"
                ) from exc
            raise

    # ---- startup ------------------------------------------------------------------------------

    async def bootstrap(self) -> str:
        """Create the MVP session, its agent ("Huddle AI"), membership and budget row if missing.
        Idempotent and serialised across replicas. Returns the agent id."""
        s = self.settings
        now = self.clock()
        async with self.tx() as conn:
            await conn.execute(
                sa.text("SELECT pg_advisory_xact_lock(:k)"), {"k": ADVISORY_LOCK_KEY}
            )
            await conn.execute(
                pg_insert(chat_sessions)
                .values(id=s.session_id, title=s.session_title, status="active")
                .on_conflict_do_nothing()
            )
            agent = await store.session_agent(conn, s.session_id)
            if agent is None:
                agent_id = await store.create_agent(
                    conn,
                    name=s.agent_name,
                    model=s.agent_model,
                    fallback_model=s.agent_fallback_model,
                    prompt_version=s.agent_prompt_version,
                    config=DEFAULT_AGENT_CONFIG,
                )
                await store.add_membership(conn, s.session_id, agent_id, ParticipantType.AGENT)
                await store.audit(
                    conn,
                    session_id=s.session_id,
                    actor_id=store.SYSTEM_SENDER_ID,
                    action="membership.joined",
                    target=agent_id,
                    payload={"participant_type": "agent"},
                )
                await self._system_message(conn, s.session_id, f"{s.agent_name} joined")
                log_event("agent.created", session_id=s.session_id, actor_id=agent_id)
            else:
                agent_id = agent.id
            await store.ensure_budget(
                conn,
                s.session_id,
                agent_id,
                capacity_tokens=s.budget_capacity_tokens,
                window_seconds=s.budget_window_seconds,
                now=now,
            )
        return agent_id

    # ---- auth ---------------------------------------------------------------------------------

    async def guest_join(self, display_name: str) -> IssuedTokens:
        """D-010 guest identity: a new user who joins the single MVP session straight to active."""
        name = normalize_name(display_name)
        sid = self.settings.session_id
        now = self.clock()
        async with self.tx() as conn:
            if await store.get_session(conn, sid) is None:
                raise ChatError(ErrorCode.INTERNAL, "session is not provisioned")
            user_id = await store.create_user(conn, name)
            await store.add_membership(conn, sid, user_id, ParticipantType.USER)
            await store.audit(
                conn,
                session_id=sid,
                actor_id=user_id,
                action="membership.joined",
                target=user_id,
                payload={"participant_type": "user"},
            )
            tokens = await self._issue(conn, user_id, new_id("rtf"), now)
            note = await self._system_message(conn, sid, f"{name} joined")
            user_ids = await store.active_user_ids(conn, sid)
        log_event("member.joined", session_id=sid, actor_id=user_id, seq=note.seq)
        joined = MemberEvent(
            type="member.joined", session_id=sid, participant_id=user_id, display_name=name
        )
        await self._broadcast(user_ids, _frame(joined), session_id=sid, event="member.joined")
        await self._broadcast_message(user_ids, note)
        return tokens

    async def refresh(self, token: str) -> IssuedTokens:
        """Rotate the refresh token (Deep Dive §8). Reusing an already-rotated token revokes the
        whole token family, since it means the token leaked."""
        now = self.clock()
        outcome = "invalid"
        issued: IssuedTokens | None = None
        user_id: str | None = None
        async with self.tx() as conn:
            row = await store.lock_refresh_token(conn, auth.hash_refresh_token(token))
            if row is not None and row.revoked_at is None:
                user_id = row.user_id
                if row.used_at is not None:
                    await store.revoke_family(conn, row.family_id, now)
                    outcome = "reuse"
                elif row.expires_at <= now:
                    outcome = "expired"
                else:
                    await store.mark_refresh_used(conn, row.id, now)
                    issued = await self._issue(conn, row.user_id, row.family_id, now)
                    outcome = "ok"
        if issued is not None:
            log_event("auth.refreshed", actor_id=issued.user_id)
            return issued
        log_event("auth.refresh_rejected", level=logging.WARNING, actor_id=user_id, reason=outcome)
        if outcome == "expired":
            raise ChatError(ErrorCode.TOKEN_EXPIRED, "refresh token expired")
        raise ChatError(ErrorCode.UNAUTHENTICATED, "invalid refresh token")

    async def _issue(
        self, conn: AsyncConnection, user_id: str, family_id: str, now: datetime
    ) -> IssuedTokens:
        s = self.settings
        refresh = auth.new_refresh_token()
        await store.store_refresh_token(
            conn,
            user_id=user_id,
            family_id=family_id,
            token_hash=auth.hash_refresh_token(refresh),
            expires_at=now + timedelta(seconds=s.refresh_ttl_seconds),
        )
        access = auth.issue_access_token(user_id, s.jwt_secret, s.access_ttl_seconds, now)
        return IssuedTokens(user_id=user_id, access_token=access, refresh_token=refresh)

    # ---- submit_message -----------------------------------------------------------------------

    async def submit_message(self, req: SubmitMessageRequest) -> SubmitMessageResponse:
        started = time.perf_counter()
        ctx = TraceContext.parse(req.trace_id)
        if ctx is not None:
            current_trace.set(ctx.child())
        sid, sender = req.session_id, req.sender
        try:
            return await self._submit(req, started)
        except ChatError as err:
            err.client_msg_id = req.client_msg_id
            log_event(
                "message.rejected",
                level=logging.WARNING,
                session_id=sid,
                actor_id=sender.id,
                latency_ms=(time.perf_counter() - started) * 1000,
                code=err.code.value,
            )
            raise

    async def _submit(self, req: SubmitMessageRequest, started: float) -> SubmitMessageResponse:
        sid, sender = req.session_id, req.sender
        if sender.type == SenderType.SYSTEM:
            raise ChatError(ErrorCode.VALIDATION, "system messages are written by the chat service")
        cmid = store.canonical_uuid(req.client_msg_id)
        if cmid is None:
            raise ChatError(ErrorCode.VALIDATION, "client_msg_id must be a UUID")
        if not req.body.strip():
            raise ChatError(ErrorCode.VALIDATION, "message body is empty")
        limit = MAX_BODY_CHARS if sender.type == SenderType.USER else AGENT_MAX_BODY_CHARS
        if len(req.body) > limit:
            raise ChatError(
                ErrorCode.MESSAGE_TOO_LONG, f"messages are limited to {limit} characters"
            )
        expected_type = "user" if sender.type == SenderType.USER else "agent"

        # Fast path for retries and non-members, outside the write transaction.
        async with self.tx() as conn:
            existing = await store.find_message(conn, sid, sender.id, cmid)
            member = None if existing else await store.get_membership(conn, sid, sender.id)
        if existing is not None:
            return self._duplicate(existing, started)
        if member is None or not member.is_active or member.participant_type != expected_type:
            raise ChatError(ErrorCode.NOT_MEMBER, "not a member of this session")

        await self._check_rate_limit(sid, sender.id, cmid)

        human = sender.type == SenderType.USER
        now = self.clock()
        row: Row[Any] | None = None
        user_ids: list[str] = []
        async with self.tx() as conn:
            # Authoritative membership check inside the transaction; FOR SHARE blocks a concurrent
            # removal until we commit, and a removal that committed first is seen here (§7).
            member = await store.get_membership(conn, sid, sender.id, lock="share")
            if member is None or not member.is_active or member.participant_type != expected_type:
                raise ChatError(ErrorCode.NOT_MEMBER, "not a member of this session")
            session = await store.lock_session(conn, sid)
            if session is None:
                raise ChatError(ErrorCode.NOT_MEMBER, "not a member of this session")
            if session.status != "active":
                raise ChatError(ErrorCode.SESSION_CLOSED, "session is closed")
            existing = await store.find_message(conn, sid, sender.id, cmid)
            if existing is None:
                seq = await store.next_seq(conn, sid, human_at=now if human else None)
                row = await store.insert_message(
                    conn,
                    session_id=sid,
                    seq=seq,
                    sender_type=sender.type,
                    sender_id=sender.id,
                    client_msg_id=cmid,
                    kind=MessageKind.TEXT,
                    body=req.body,
                    agent_run_id=None if human else cmid,
                )
                user_ids = await store.active_user_ids(conn, sid)
        if row is None:
            if existing is None:  # unreachable: one of the two branches above ran
                raise ChatError(ErrorCode.INTERNAL, "message was not stored")
            return self._duplicate(existing, started)

        message = store.to_message(row)
        log_event(
            "message.committed",
            session_id=sid,
            actor_id=sender.id,
            message_id=message.id,
            seq=message.seq,
            latency_ms=(time.perf_counter() - started) * 1000,
            sender_type=sender.type.value,
        )
        await self._broadcast_message(user_ids, message)
        if human:  # Agent (and system) messages never trigger the agent (Deep Dive §3, §5).
            await self._enqueue_trigger(message)
        return SubmitMessageResponse(message_id=message.id, seq=message.seq)

    def _duplicate(self, row: Row[Any], started: float) -> SubmitMessageResponse:
        log_event(
            "message.duplicate",
            session_id=row.session_id,
            actor_id=row.sender_id,
            message_id=row.id,
            seq=row.seq,
            latency_ms=(time.perf_counter() - started) * 1000,
        )
        return SubmitMessageResponse(message_id=row.id, seq=row.seq, duplicate=True)

    async def _check_rate_limit(self, session_id: str, sender_id: str, cmid: str) -> None:
        s = self.settings
        try:
            wait_ms = await self.redis.rate_limit(
                session_id, sender_id, cmid, s.rate_limit_messages, s.rate_limit_window_ms
            )
        except (RedisError, OSError) as exc:
            # The limiter is abuse protection, not correctness: fail open when Redis is down.
            log_event("rate_limit_unavailable", level=logging.WARNING, error=type(exc).__name__)
            return
        if wait_ms > 0:
            raise ChatError(
                ErrorCode.RATE_LIMITED, "too many messages, slow down", retry_after_ms=wait_ms
            )

    async def _system_message(self, conn: AsyncConnection, session_id: str, body: str) -> Message:
        """A kind=system line (joins, pauses, renames) through the same seq path (Deep Dive §4)."""
        seq = await store.next_seq(conn, session_id)
        row = await store.insert_message(
            conn,
            session_id=session_id,
            seq=seq,
            sender_type=SenderType.SYSTEM,
            sender_id=store.SYSTEM_SENDER_ID,
            client_msg_id=str(uuid.uuid4()),
            kind=MessageKind.SYSTEM,
            body=body,
        )
        return store.to_message(row)

    # ---- fan-out and triggers (after commit) ---------------------------------------------------

    async def _broadcast(
        self, user_ids: list[str], frame: dict[str, Any], *, session_id: str, event: str
    ) -> None:
        """Pub/sub fan-out can drop; cursors recover (D-007). Never fails the caller."""
        started = time.perf_counter()
        try:
            targets = await self.redis.connections_by_gateway(user_ids)
            gateways = await self.redis.publish(targets, frame)
        except (RedisError, OSError) as exc:
            log_event(
                "fanout_publish_errors",
                level=logging.WARNING,
                session_id=session_id,
                frame=event,
                error=type(exc).__name__,
            )
            return
        log_event(
            "fanout.published",
            level=logging.DEBUG,
            session_id=session_id,
            latency_ms=(time.perf_counter() - started) * 1000,
            frame=event,
            gateways=gateways,
            connections=sum(len(c) for c in targets.values()),
        )

    async def _broadcast_message(self, user_ids: list[str], message: Message) -> None:
        frame = MessageCreated(
            event_id=new_id("evt"),
            trace_id=trace().traceparent,
            session_id=message.session_id,
            sequence=message.seq,
            message=message,
        )
        await self._broadcast(
            user_ids, _frame(frame), session_id=message.session_id, event="message.created"
        )

    async def _enqueue_trigger(self, message: Message) -> None:
        trigger = AgentTrigger(
            session_id=message.session_id,
            seq=message.seq,
            message_id=message.id,
            sender_type=message.sender_type,
            trace_id=trace().traceparent,
        )
        try:
            await self.redis.enqueue_trigger(trigger)
        except (RedisError, OSError) as exc:
            # A lost trigger only delays the agent until the next message (Deep Dive §7).
            log_event(
                "trigger_enqueue_failed",
                level=logging.WARNING,
                session_id=message.session_id,
                message_id=message.id,
                seq=message.seq,
                error=type(exc).__name__,
            )

    # ---- receipts -----------------------------------------------------------------------------

    async def ack_delivery(self, req: AckDeliveryRequest) -> AckResult:
        """Monotonic cursors (D-003). `read` implies delivered. Broadcast receipt.updated when a
        delivered cursor moves; an inactive agent's cursor does not move (D-020)."""
        if req.kind not in ACK_KINDS:
            raise ChatError(ErrorCode.VALIDATION, "kind must be delivered or read")
        if req.up_to_seq < 0:
            raise ChatError(ErrorCode.VALIDATION, "up_to_seq must be >= 0")
        sid, pid = req.session_id, req.participant_id
        now = self.clock()
        cursor_map: dict[str, int] | None = None
        user_ids: list[str] = []
        async with self.tx() as conn:
            member = await store.get_membership(conn, sid, pid, lock="update")
            session = await store.get_session(conn, sid)
            if member is None or not member.is_active or session is None:
                raise ChatError(ErrorCode.NOT_MEMBER, "not a member of this session")
            if member.participant_type == "agent" and not await self._agent_can_read(
                conn, sid, now
            ):
                log_event("ack.ignored_inactive_agent", session_id=sid, actor_id=pid)
                return AckResult(
                    last_delivered_seq=member.last_delivered_seq,
                    last_read_seq=member.last_read_seq,
                    advanced=False,
                )
            up_to = min(req.up_to_seq, int(session.last_seq))  # clients are untrusted
            delivered = max(member.last_delivered_seq, up_to)
            read = max(member.last_read_seq, up_to) if req.kind == "read" else member.last_read_seq
            moved = delivered != member.last_delivered_seq
            if moved or read != member.last_read_seq:
                await store.advance_cursors(conn, sid, pid, delivered, read)
            if moved:
                cursor_map = await store.cursors(conn, sid)
                user_ids = await store.active_user_ids(conn, sid)
        if cursor_map is not None:
            log_event("receipt.advanced", session_id=sid, actor_id=pid, seq=delivered)
            frame = ReceiptUpdated(session_id=sid, cursors=cursor_map)
            await self._broadcast(user_ids, _frame(frame), session_id=sid, event="receipt.updated")
        return AckResult(last_delivered_seq=delivered, last_read_seq=read, advanced=moved)

    async def _agent_can_read(self, conn: AsyncConnection, session_id: str, now: datetime) -> bool:
        agent = await store.session_agent(conn, session_id)
        if agent is None or agent.status != "active":
            return False
        budget = await store.budget(conn, session_id, agent.id, now)
        return budget is None or not budget.resting

    # ---- reads ---------------------------------------------------------------------------------

    @staticmethod
    def _check_page(after_seq: int | None, before_seq: int | None, limit: int) -> None:
        if not 1 <= limit <= MAX_PAGE:
            raise ChatError(ErrorCode.VALIDATION, f"limit must be between 1 and {MAX_PAGE}")
        if (after_seq is not None and after_seq < 0) or (before_seq is not None and before_seq < 0):
            raise ChatError(ErrorCode.VALIDATION, "seq cursors must be >= 0")

    async def page(
        self,
        session_id: str,
        *,
        after_seq: int | None,
        before_seq: int | None,
        limit: int,
        member_id: str | None = None,
    ) -> MessagePage:
        self._check_page(after_seq, before_seq, limit)
        async with self.tx() as conn:
            if member_id is not None:
                await self._require_member(conn, session_id, member_id)
            msgs, has_more = await store.page_messages(
                conn, session_id, after_seq=after_seq, before_seq=before_seq, limit=limit
            )
        return MessagePage(messages=msgs, has_more=has_more)

    async def membership(self, session_id: str, participant_id: str) -> MembershipCheck:
        async with self.tx() as conn:
            member = await store.get_membership(conn, session_id, participant_id)
            name = await store.display_name(conn, participant_id) if member else None
        if member is None:
            return MembershipCheck(is_active=False)
        return MembershipCheck(
            is_active=member.is_active,
            participant_type=member.participant_type,
            display_name=name,
            last_delivered_seq=member.last_delivered_seq,
        )

    async def _require_member(self, conn: AsyncConnection, session_id: str, user_id: str) -> None:
        member = await store.get_membership(conn, session_id, user_id)
        if member is None or not member.is_active or member.participant_type != "user":
            raise ChatError(ErrorCode.NOT_MEMBER, "not a member of this session")

    async def session_view(self, session_id: str, user_id: str) -> SessionView:
        now = self.clock()
        async with self.tx() as conn:
            await self._require_member(conn, session_id, user_id)
            session = await store.get_session(conn, session_id)
            if session is None:
                raise ChatError(ErrorCode.NOT_MEMBER, "not a member of this session")
            members = await store.members_view(conn, session_id)
            agent = await self._agent_view(conn, session_id, now)
        return SessionView(
            id=session.id,
            title=session.title,
            status=session.status,
            last_seq=session.last_seq,
            members=members,
            agent=agent,
        )

    # ---- agent ---------------------------------------------------------------------------------

    async def _reported_state(self, session_id: str) -> AgentState | None:
        try:
            return await self.redis.reported_state(session_id)
        except (RedisError, OSError):
            return None

    async def _budget_row(
        self, conn: AsyncConnection, session_id: str, agent_id: str, now: datetime, add: int = 0
    ) -> store.BudgetRow:
        row = await store.budget(conn, session_id, agent_id, now, add_tokens=add)
        if row is None:
            s = self.settings
            await store.ensure_budget(
                conn,
                session_id,
                agent_id,
                capacity_tokens=s.budget_capacity_tokens,
                window_seconds=s.budget_window_seconds,
                now=now,
            )
            row = await store.budget(conn, session_id, agent_id, now, add_tokens=add)
        if row is None:
            raise ChatError(ErrorCode.INTERNAL, "agent budget is missing")
        return row

    async def _agent_view(
        self, conn: AsyncConnection, session_id: str, now: datetime
    ) -> AgentView | None:
        agent = await store.session_agent(conn, session_id)
        if agent is None:
            return None
        return await self._view_of(conn, session_id, agent, now)

    async def _required_agent_view(
        self, conn: AsyncConnection, session_id: str, now: datetime
    ) -> AgentView:
        agent = await self._require_agent(conn, session_id)
        return await self._view_of(conn, session_id, agent, now)

    async def _view_of(
        self, conn: AsyncConnection, session_id: str, agent: store.AgentRow, now: datetime
    ) -> AgentView:
        budget = await self._budget_row(conn, session_id, agent.id, now)
        state = effective_state(
            agent.status, await self._reported_state(session_id), budget.resting
        )
        return AgentView(
            id=agent.id,
            name=agent.name,
            state=state,
            budget=BudgetView(used_pct=budget.used_pct, refills_at=budget.refills_at),
        )

    async def _require_agent(self, conn: AsyncConnection, session_id: str) -> store.AgentRow:
        agent = await store.session_agent(conn, session_id)
        if agent is None:
            raise ChatError(ErrorCode.NOT_MEMBER, "the session has no agent")
        return agent

    async def agent_view(self, session_id: str, user_id: str) -> AgentView:
        now = self.clock()
        async with self.tx() as conn:
            await self._require_member(conn, session_id, user_id)
            return await self._required_agent_view(conn, session_id, now)

    async def patch_agent(
        self, session_id: str, user_id: str, *, status: str | None, name: str | None
    ) -> AgentView:
        """Pause, resume or rename by any member in the MVP (Deep Dive §5, §8). Each change writes
        an audit event and a system note, then agent.status is fanned out."""
        target: str | None = None
        if status is not None:
            aliases = {"paused": "paused", "active": "active", "following": "active"}
            target = aliases.get(status)
            if target is None:
                raise ChatError(ErrorCode.VALIDATION, "status must be paused or active")
        new_name = normalize_name(name) if name is not None else None
        if target is None and new_name is None:
            raise ChatError(ErrorCode.VALIDATION, "nothing to change")
        now = self.clock()
        notes: list[Message] = []
        async with self.tx() as conn:
            member = await store.get_membership(conn, session_id, user_id, lock="share")
            if member is None or not member.is_active or member.participant_type != "user":
                raise ChatError(ErrorCode.NOT_MEMBER, "not a member of this session")
            agent = await store.session_agent(conn, session_id, lock=True)
            if agent is None:
                raise ChatError(ErrorCode.NOT_MEMBER, "the session has no agent")
            actor = await store.display_name(conn, user_id) or "Someone"
            if target is not None and target != agent.status:
                await store.update_agent(conn, agent.id, status=target)
                verb = "paused" if target == "paused" else "resumed"
                await store.audit(
                    conn,
                    session_id=session_id,
                    actor_id=user_id,
                    action=f"agent.{verb}",
                    target=agent.id,
                    payload={"from": agent.status, "to": target},
                )
                notes.append(
                    await self._system_message(conn, session_id, f"{actor} {verb} {agent.name}")
                )
            if new_name is not None and new_name != agent.name:
                await store.update_agent(conn, agent.id, name=new_name)
                await store.audit(
                    conn,
                    session_id=session_id,
                    actor_id=user_id,
                    action="agent.renamed",
                    target=agent.id,
                    payload={"from": agent.name, "to": new_name},
                )
                notes.append(
                    await self._system_message(
                        conn, session_id, f"{actor} renamed the AI to {new_name}"
                    )
                )
            view = await self._view_of(
                conn, session_id, await self._require_agent(conn, session_id), now
            )
            user_ids = await store.active_user_ids(conn, session_id)
        for note in notes:
            log_event("agent.updated", session_id=session_id, actor_id=user_id, seq=note.seq)
            await self._broadcast_message(user_ids, note)
        if notes:
            await self._broadcast_status(user_ids, session_id, view)
        return view

    async def _broadcast_status(
        self, user_ids: list[str], session_id: str, view: AgentView
    ) -> None:
        frame = AgentStatus(
            session_id=session_id, state=view.state, name=view.name, budget=view.budget
        )
        await self._broadcast(user_ids, _frame(frame), session_id=session_id, event="agent.status")

    async def set_agent_status(self, session_id: str, state: AgentState) -> AgentView:
        """Orchestrator-reported live state; users see the derived effective state."""
        try:
            await self.redis.set_reported_state(session_id, state)
        except (RedisError, OSError) as exc:
            log_event("agent_state_unavailable", level=logging.WARNING, error=type(exc).__name__)
        now = self.clock()
        async with self.tx() as conn:
            view = await self._required_agent_view(conn, session_id, now)
            user_ids = await store.active_user_ids(conn, session_id)
        log_event(
            "agent.status", session_id=session_id, reported=state.value, state=view.state.value
        )
        await self._broadcast_status(user_ids, session_id, view)
        return view

    def _budget_state(self, row: store.BudgetRow) -> BudgetState:
        return BudgetState(
            capacity_tokens=row.capacity_tokens,
            used_tokens=row.used_tokens,
            window_seconds=row.window_seconds,
            window_started_at=row.window_started_at,
            refills_at=row.refills_at,
            view=BudgetView(used_pct=row.used_pct, refills_at=row.refills_at),
        )

    async def budget_state(self, session_id: str) -> BudgetState:
        now = self.clock()
        async with self.tx() as conn:
            agent = await self._require_agent(conn, session_id)
            row = await self._budget_row(conn, session_id, agent.id, now)
        return self._budget_state(row)

    async def record_usage(self, session_id: str, req: RecordUsageRequest) -> BudgetState:
        """Debit the fixed-window budget (D-019). agent.status is re-broadcast when the visible
        percentage or the resting state changes."""
        now = self.clock()
        async with self.tx() as conn:
            agent = await self._require_agent(conn, session_id)
            row = await self._budget_row(conn, session_id, agent.id, now, add=req.tokens)
            view: AgentView | None = None
            # Compare with what users last saw, so a window roll (resting -> following) shows.
            if row.prior_visible != (row.used_pct, row.resting):
                view = await self._view_of(conn, session_id, agent, now)
            user_ids = await store.active_user_ids(conn, session_id) if view else []
        log_event(
            "budget.debited",
            session_id=session_id,
            actor_id=agent.id,
            run_id=req.run_id,
            tokens=req.tokens,
            used_pct=row.used_pct,
        )
        if view is not None:
            await self._broadcast_status(user_ids, session_id, view)
        return self._budget_state(row)
