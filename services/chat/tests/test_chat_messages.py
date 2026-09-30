"""submit_message against real Postgres/Redis (Deep Dive §2, §3, §7, §11)."""

from __future__ import annotations

import asyncio
import uuid

import pytest
import sqlalchemy as sa
from chat_testkit import Clock, triggers_for
from redis.asyncio import Redis
from sqlalchemy.exc import IntegrityError

from huddle_chat import store
from huddle_chat.errors import ChatError
from huddle_chat.schema import agent_runs, chat_sessions, messages, session_memberships
from huddle_chat.service import ChatService
from huddle_contracts.errors import ErrorCode
from huddle_contracts.internal import Sender, SubmitMessageRequest, SubmitMessageResponse
from huddle_contracts.models import MessageKind, SenderType

pytestmark = pytest.mark.integration


async def join(service: ChatService, name: str) -> str:
    return (await service.guest_join(name)).user_id


async def agent_id(service: ChatService) -> str:
    async with service.tx() as conn:
        agent = await store.session_agent(conn, service.settings.session_id)
    assert agent is not None
    return agent.id


async def send(
    service: ChatService,
    sender_id: str,
    body: str = "hello",
    *,
    cmid: str | None = None,
    sender_type: SenderType = SenderType.USER,
) -> SubmitMessageResponse:
    return await service.submit_message(
        SubmitMessageRequest(
            session_id=service.settings.session_id,
            sender=Sender(type=sender_type, id=sender_id),
            client_msg_id=cmid or str(uuid.uuid4()),
            body=body,
        )
    )


async def last_seq(service: ChatService) -> int:
    async with service.tx() as conn:
        row = await store.get_session(conn, service.settings.session_id)
    assert row is not None
    return int(row.last_seq)


async def seqs(service: ChatService) -> list[int]:
    async with service.tx() as conn:
        rows = await conn.execute(
            sa.select(messages.c.seq)
            .where(messages.c.session_id == service.settings.session_id)
            .order_by(messages.c.seq)
        )
        return [int(r.seq) for r in rows]


# ---- ordering ----------------------------------------------------------------------------------


async def test_bootstrap_is_idempotent(service: ChatService) -> None:
    first = await agent_id(service)
    before = await last_seq(service)
    assert await service.bootstrap() == first
    assert await last_seq(service) == before == 1  # one "Huddle AI joined" note


async def test_seq_is_gapless_under_concurrent_sends(service: ChatService) -> None:
    users = [await join(service, f"User {i}") for i in range(5)]
    start = await last_seq(service)
    results = await asyncio.gather(
        *(send(service, uid, f"m{i}") for uid in users for i in range(10))
    )
    got = sorted(r.seq for r in results)
    assert got == list(range(start + 1, start + 51))
    assert await last_seq(service) == start + 50
    all_seqs = await seqs(service)
    assert all_seqs == list(range(1, len(all_seqs) + 1))


async def test_seq_counter_rolls_back_with_the_transaction(service: ChatService) -> None:
    uid = await join(service, "Ana")
    before = await last_seq(service)
    with pytest.raises(IntegrityError):
        async with service.engine.begin() as conn:
            seq = await store.next_seq(conn, service.settings.session_id)
            await store.insert_message(
                conn,
                session_id=service.settings.session_id,
                seq=seq,
                sender_type=SenderType.USER,
                sender_id=uid,
                client_msg_id=str(uuid.uuid4()),
                kind=MessageKind.TEXT,
                body="x" * 4001,  # violates the body CHECK after the increment
            )
    assert await last_seq(service) == before
    assert (await send(service, uid)).seq == before + 1


# ---- idempotency -------------------------------------------------------------------------------


async def test_repeated_client_msg_id_returns_the_original(
    service: ChatService, redis: Redis
) -> None:
    uid = await join(service, "Ana")
    cmid = str(uuid.uuid4())
    first = await send(service, uid, cmid=cmid)
    again = await send(service, uid, "different body on retry", cmid=cmid.upper())
    assert (again.message_id, again.seq, again.duplicate) == (first.message_id, first.seq, True)
    assert not first.duplicate
    assert await last_seq(service) == first.seq
    assert len(await triggers_for(redis, service.settings.session_id)) == 1


async def test_concurrent_duplicates_commit_once(service: ChatService) -> None:
    uid = await join(service, "Ana")
    cmid = str(uuid.uuid4())
    results = await asyncio.gather(*(send(service, uid, cmid=cmid) for _ in range(8)))
    assert len({(r.message_id, r.seq) for r in results}) == 1
    assert sum(not r.duplicate for r in results) == 1


async def test_same_client_msg_id_from_different_senders_is_not_a_duplicate(
    service: ChatService,
) -> None:
    a, b = await join(service, "Ana"), await join(service, "Ben")
    cmid = str(uuid.uuid4())
    ra, rb = await send(service, a, cmid=cmid), await send(service, b, cmid=cmid)
    assert ra.message_id != rb.message_id and not rb.duplicate


async def test_agent_run_id_is_the_idempotency_key(service: ChatService, redis: Redis) -> None:
    aid = await agent_id(service)
    run_id = str(uuid.uuid4())
    # the orchestrator records its run before committing; the message links to it
    async with service.engine.begin() as conn:
        await conn.execute(
            sa.insert(agent_runs).values(
                id=run_id,
                session_id=service.settings.session_id,
                agent_id=aid,
                trigger_kind="message",
                trigger_seq=1,
                context_seq=1,
                status="validated",
            )
        )
    first = await send(service, aid, "A thought.", cmid=run_id, sender_type=SenderType.AGENT)
    retry = await send(service, aid, "A thought.", cmid=run_id, sender_type=SenderType.AGENT)
    assert retry.duplicate and (retry.message_id, retry.seq) == (first.message_id, first.seq)
    async with service.tx() as conn:
        linked = (
            await conn.execute(
                sa.select(messages.c.agent_run_id).where(messages.c.id == first.message_id)
            )
        ).scalar_one()
    assert linked == run_id
    # agent messages never trigger the agent
    assert await triggers_for(redis, service.settings.session_id) == []


async def test_agent_message_without_recorded_run_still_commits(service: ChatService) -> None:
    aid = await agent_id(service)
    r = await send(service, aid, "Hi.", sender_type=SenderType.AGENT)
    assert r.seq == await last_seq(service)


# ---- authorization -----------------------------------------------------------------------------


async def test_non_member_and_wrong_sender_type_are_rejected(service: ChatService) -> None:
    uid = await join(service, "Ana")
    for sender_id, sender_type in (
        ("usr_stranger", SenderType.USER),
        (uid, SenderType.AGENT),  # a user cannot speak as an agent
        (await agent_id(service), SenderType.USER),
    ):
        with pytest.raises(ChatError) as e:
            await send(service, sender_id, sender_type=sender_type)
        assert e.value.code == ErrorCode.NOT_MEMBER


async def _set_status(service: ChatService, uid: str, status: str) -> None:
    async with service.engine.begin() as conn:
        await conn.execute(
            sa.update(session_memberships)
            .where(
                session_memberships.c.session_id == service.settings.session_id,
                session_memberships.c.participant_id == uid,
            )
            .values(status=status)
        )


async def test_removed_member_is_rejected(service: ChatService) -> None:
    uid = await join(service, "Ana")
    await _set_status(service, uid, "removed")
    with pytest.raises(ChatError) as e:
        await send(service, uid)
    assert e.value.code == ErrorCode.NOT_MEMBER


async def test_member_removed_while_message_in_flight_is_rejected_inside_the_txn(
    service: ChatService,
) -> None:
    """Deep Dive §7: the pre-check passes, then the removal commits while the send waits on the
    membership row; the check inside the send transaction sees the removal."""
    uid = await join(service, "Ana")
    before = await last_seq(service)
    m = session_memberships.c
    async with service.engine.connect() as blocker:
        await blocker.execute(
            sa.select(m.status)
            .where(m.session_id == service.settings.session_id, m.participant_id == uid)
            .with_for_update()
        )
        task = asyncio.create_task(send(service, uid))
        await asyncio.sleep(0.5)
        assert not task.done()  # waiting on FOR SHARE inside the send transaction
        await blocker.execute(
            sa.update(session_memberships)
            .where(m.session_id == service.settings.session_id, m.participant_id == uid)
            .values(status="removed")
        )
        await blocker.commit()
    with pytest.raises(ChatError) as e:
        await task
    assert e.value.code == ErrorCode.NOT_MEMBER
    assert await last_seq(service) == before


async def test_closed_session_rejects_sends(service: ChatService) -> None:
    uid = await join(service, "Ana")
    async with service.engine.begin() as conn:
        await conn.execute(
            sa.update(chat_sessions)
            .where(chat_sessions.c.id == service.settings.session_id)
            .values(status="closed")
        )
    with pytest.raises(ChatError) as e:
        await send(service, uid)
    assert e.value.code == ErrorCode.SESSION_CLOSED


# ---- limits ------------------------------------------------------------------------------------


async def test_rate_limit_ten_per_ten_seconds(service: ChatService) -> None:
    uid, other = await join(service, "Ana"), await join(service, "Ben")
    cmids = [str(uuid.uuid4()) for _ in range(10)]
    for i, cmid in enumerate(cmids):
        await send(service, uid, f"m{i}", cmid=cmid)
    with pytest.raises(ChatError) as e:
        await send(service, uid, "one too many")
    assert e.value.code == ErrorCode.RATE_LIMITED
    assert e.value.retry_after_ms is not None and 0 < e.value.retry_after_ms <= 10_000
    assert e.value.body().retryable
    # retrying an already-committed message is not limited, and the limit is per user
    assert (await send(service, uid, cmid=cmids[0])).duplicate
    assert not (await send(service, other)).duplicate


async def test_length_limits(service: ChatService) -> None:
    uid, aid = await join(service, "Ana"), await agent_id(service)
    assert (await send(service, uid, "x" * 4000)).seq > 0
    assert (await send(service, aid, "y" * 1000, sender_type=SenderType.AGENT)).seq > 0
    with pytest.raises(ChatError) as e:
        await send(service, aid, "y" * 1001, sender_type=SenderType.AGENT)
    assert e.value.code == ErrorCode.MESSAGE_TOO_LONG


# ---- after commit: triggers ----------------------------------------------------------------------


async def test_trigger_only_for_human_messages(service: ChatService, redis: Redis) -> None:
    sid = service.settings.session_id
    uid, aid = await join(service, "Ana"), await agent_id(service)
    human = await send(service, uid, "What do we think?")
    await send(service, aid, "One idea.", sender_type=SenderType.AGENT)
    await service.patch_agent(sid, uid, status="paused", name=None)  # a system note
    got = await triggers_for(redis, sid)
    assert len(got) == 1
    t = got[0]
    assert (t["seq"], t["message_id"], t["sender_type"]) == (
        str(human.seq),
        human.message_id,
        "user",
    )
    assert t["trace_id"].startswith("00-")


async def test_last_human_message_at_tracks_humans_only(service: ChatService, clock: Clock) -> None:
    sid = service.settings.session_id
    uid, aid = await join(service, "Ana"), await agent_id(service)

    async def last_human() -> object:
        async with service.tx() as conn:
            row = await store.get_session(conn, sid)
        assert row is not None
        return row.last_human_message_at

    assert await last_human() is None
    await send(service, uid)
    stamped = await last_human()
    assert stamped == clock()
    clock.advance(60)
    await send(service, aid, "Hm.", sender_type=SenderType.AGENT)
    assert await last_human() == stamped


async def test_paging(service: ChatService) -> None:
    sid = service.settings.session_id
    uid = await join(service, "Ana")
    for i in range(7):
        await send(service, uid, f"m{i}")
    total = await last_seq(service)  # 2 join notes + 7
    page = await service.page(sid, after_seq=0, before_seq=None, limit=4)
    assert [m.seq for m in page.messages] == [1, 2, 3, 4] and page.has_more
    page = await service.page(sid, after_seq=4, before_seq=None, limit=200)
    assert [m.seq for m in page.messages] == list(range(5, total + 1)) and not page.has_more
    page = await service.page(sid, after_seq=None, before_seq=None, limit=3)
    assert [m.seq for m in page.messages] == [total - 2, total - 1, total] and page.has_more
    page = await service.page(sid, after_seq=None, before_seq=3, limit=5)
    assert [m.seq for m in page.messages] == [1, 2] and not page.has_more
    with pytest.raises(ChatError):
        await service.page(sid, after_seq=0, before_seq=None, limit=201)
    with pytest.raises(ChatError) as e:
        await service.page(sid, after_seq=0, before_seq=None, limit=10, member_id="usr_nobody")
    assert e.value.code == ErrorCode.NOT_MEMBER
