"""Fan-out through the connection registry, and receipt cursors (Deep Dive §2, §4, §7, §11)."""

from __future__ import annotations

import uuid

import pytest
from chat_testkit import FakeGateways
from redis.asyncio import Redis

from huddle_chat import store
from huddle_chat.errors import ChatError
from huddle_chat.service import ChatService
from huddle_contracts import redis_keys as keys
from huddle_contracts.errors import ErrorCode
from huddle_contracts.internal import AckDeliveryRequest, Sender, SubmitMessageRequest
from huddle_contracts.models import AgentState, SenderType

pytestmark = pytest.mark.integration


async def send(service: ChatService, uid: str, body: str = "hi") -> int:
    r = await service.submit_message(
        SubmitMessageRequest(
            session_id=service.settings.session_id,
            sender=Sender(type=SenderType.USER, id=uid),
            client_msg_id=str(uuid.uuid4()),
            body=body,
        )
    )
    return r.seq


async def ack(service: ChatService, pid: str, up_to: int, kind: str = "delivered") -> bool:
    res = await service.ack_delivery(
        AckDeliveryRequest(
            participant_id=pid,
            device_id="dev",
            session_id=service.settings.session_id,
            up_to_seq=up_to,
            kind=kind,
        )
    )
    return res.advanced


# ---- fan-out -----------------------------------------------------------------------------------


async def test_fanout_reaches_every_device_on_live_gateways_only(
    service: ChatService, gateways: FakeGateways, redis: Redis
) -> None:
    ana = (await service.guest_join("Ana")).user_id
    ben = (await service.guest_join("Ben")).user_id
    await gateways.start("g1", "g2")
    await gateways.start("dead", alive=False)
    phone = await gateways.connect(ana, "g1")
    laptop = await gateways.connect(ana, "g2")
    ben_tab = await gateways.connect(ben, "g1")
    on_dead = await gateways.connect(ben, "dead")
    expired = await gateways.connect(ben, "g1", ttl=-5)  # missed heartbeats: score in the past
    hashless = await gateways.connect(ana, "g2", live=False)  # conn: hash already expired

    seq = await send(service, ana, "hello all")
    await gateways.drain()

    g1 = gateways.frames("g1", "message.created")
    g2 = gateways.frames("g2", "message.created")
    assert len(g1) == 1 and len(g2) == 1
    assert sorted(g1[0]["connection_ids"]) == sorted([phone, ben_tab])
    assert g2[0]["connection_ids"] == [laptop]
    frame = g1[0]["frame"]
    assert frame["sequence"] == seq == frame["message"]["seq"]
    assert frame["message"]["body"] == "hello all" and frame["v"] == 1
    assert frame["trace_id"].startswith("00-")
    assert gateways.frames("dead") == []  # gw_alive expired: skipped
    # the dead gateway's live entry stays (its own TTL cleans it); stale entries are pruned
    ben_conns = await redis.zscore(keys.user_conns(ben), on_dead)
    assert ben_conns is not None
    assert await redis.zscore(keys.user_conns(ben), expired) is None
    assert await redis.zscore(keys.user_conns(ana), hashless) is None


async def test_fanout_failure_never_fails_a_committed_send(
    service: ChatService, gateways: FakeGateways
) -> None:
    ana = (await service.guest_join("Ana")).user_id
    good_redis = service.redis.redis
    service.redis.redis = Redis.from_url("redis://127.0.0.1:1/0")  # pyright: ignore[reportUnknownMemberType]
    try:
        seq = await send(service, ana)  # fan-out, trigger and rate limit all fail open
    finally:
        await service.redis.redis.aclose()
        service.redis.redis = good_redis
    page = await service.page(
        service.settings.session_id, after_seq=seq - 1, before_seq=None, limit=5
    )
    assert [m.seq for m in page.messages] == [seq]  # stored, recoverable through the cursor


# ---- receipts ----------------------------------------------------------------------------------


async def test_cursors_are_monotonic_and_clamped(service: ChatService) -> None:
    ana = (await service.guest_join("Ana")).user_id
    ben = (await service.guest_join("Ben")).user_id
    last = 0
    for _ in range(4):
        last = await send(service, ana)
    assert await ack(service, ben, last)
    assert not await ack(service, ben, last - 2)  # stale ack: cursors only move forward
    assert not await ack(service, ben, last)  # a second device acking the same seq
    assert not await ack(service, ben, last + 100)  # clamped to last_seq, so no movement
    check = await service.membership(service.settings.session_id, ben)
    assert check.is_active and check.last_delivered_seq == last
    assert check.participant_type == "user" and check.display_name == "Ben"


async def test_read_implies_delivered(service: ChatService) -> None:
    ana = (await service.guest_join("Ana")).user_id
    ben = (await service.guest_join("Ben")).user_id
    seq = await send(service, ana)
    res = await service.ack_delivery(
        AckDeliveryRequest(
            participant_id=ben, session_id=service.settings.session_id, up_to_seq=seq, kind="read"
        )
    )
    assert (res.last_delivered_seq, res.last_read_seq, res.advanced) == (seq, seq, True)


async def test_receipt_updated_broadcast_on_advance(
    service: ChatService, gateways: FakeGateways
) -> None:
    sid = service.settings.session_id
    ana = (await service.guest_join("Ana")).user_id
    ben = (await service.guest_join("Ben")).user_id
    async with service.tx() as conn:
        agent = await store.session_agent(conn, sid)
    assert agent is not None
    await gateways.start("g1")
    await gateways.connect(ana, "g1")
    seq = await send(service, ana)
    await ack(service, ben, seq)
    await ack(service, ben, seq)  # no movement, no broadcast
    await ack(service, agent.id, seq, kind="read")  # first agent read -> amber tick
    await gateways.drain()
    receipts = [e["frame"] for e in gateways.frames("g1", "receipt.updated")]
    assert len(receipts) == 2
    assert receipts[0]["cursors"][ben] == seq
    assert receipts[1]["cursors"][agent.id] == seq and receipts[1]["cursors"][ben] == seq
    assert receipts[1]["session_id"] == sid


async def test_paused_agent_does_not_advance_its_cursor(service: ChatService) -> None:
    """D-020: an inactive agent's read cursor stays put, so messages wait at the second tick."""
    sid = service.settings.session_id
    ana = (await service.guest_join("Ana")).user_id
    async with service.tx() as conn:
        agent = await store.session_agent(conn, sid)
    assert agent is not None
    seq = await send(service, ana)
    await service.patch_agent(sid, ana, status="paused", name=None)
    assert not await ack(service, agent.id, seq, kind="read")
    assert (await service.membership(sid, agent.id)).last_delivered_seq == 0
    await service.patch_agent(sid, ana, status="active", name=None)
    assert await ack(service, agent.id, seq, kind="read")


async def test_away_agent_does_not_advance_its_cursor(service: ChatService) -> None:
    """D-020: an `away` agent has read nothing, whichever worker reports the read ack."""
    sid = service.settings.session_id
    ana = (await service.guest_join("Ana")).user_id
    async with service.tx() as conn:
        agent = await store.session_agent(conn, sid)
    assert agent is not None
    seq = await send(service, ana)
    await service.set_agent_status(sid, AgentState.AWAY)
    assert not await ack(service, agent.id, seq, kind="read")
    await service.set_agent_status(sid, AgentState.FOLLOWING)
    assert await ack(service, agent.id, seq, kind="read")


async def test_ack_validation_and_membership(service: ChatService) -> None:
    ana = (await service.guest_join("Ana")).user_id
    with pytest.raises(ChatError) as e:
        await ack(service, "usr_stranger", 1)
    assert e.value.code == ErrorCode.NOT_MEMBER
    with pytest.raises(ChatError) as e:
        await ack(service, ana, 1, kind="seen")
    assert e.value.code == ErrorCode.VALIDATION
    with pytest.raises(ChatError) as e:
        await ack(service, ana, -1)
    assert e.value.code == ErrorCode.VALIDATION
