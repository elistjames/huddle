"""HTTP surface with the real lifespan (migrate + bootstrap) against real Postgres/Redis."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from chat_testkit import SERVICE_TOKEN, FakeGateways

from huddle_chat.app import create_app
from huddle_chat.config import Settings
from huddle_chat.service import ChatService

pytestmark = pytest.mark.integration
SVC = {"X-Service-Token": SERVICE_TOKEN}


@pytest.fixture
async def http(service: ChatService, settings: Settings) -> AsyncIterator[httpx.AsyncClient]:
    """`service` has already truncated and bootstrapped this test's session."""
    app = create_app(settings)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://chat") as client:
            yield client


async def guest(http: httpx.AsyncClient, name: str) -> dict[str, Any]:
    r = await http.post("/auth/guest", json={"display_name": name})
    assert r.status_code == 200, r.text
    data: dict[str, Any] = r.json()
    data["h"] = {"Authorization": f"Bearer {data['access_token']}"}
    return data


async def test_guest_flow_session_send_and_page(
    http: httpx.AsyncClient, settings: Settings
) -> None:
    sid = settings.session_id
    ana = await guest(http, "  Ana  ")
    assert ana["user_id"].startswith("usr_") and ana["expires_in"] == 900

    r = await http.get(f"/sessions/{sid}", headers=ana["h"])
    assert r.status_code == 200
    session = r.json()
    assert session["id"] == sid and session["status"] == "active"
    names = {m["display_name"]: m["participant_type"] for m in session["members"]}
    assert names == {"Huddle AI": "agent", "Ana": "user"}
    assert session["agent"]["name"] == "Huddle AI" and session["agent"]["state"] == "following"
    assert set(session["agent"]["budget"]) == {"used_pct", "refills_at"}

    cmid = str(uuid.uuid4())
    body = {"client_msg_id": cmid, "body": "hello"}
    first = (await http.post(f"/sessions/{sid}/messages", json=body, headers=ana["h"])).json()
    again = (await http.post(f"/sessions/{sid}/messages", json=body, headers=ana["h"])).json()
    assert first["seq"] == again["seq"] and again["duplicate"] is True

    r = await http.get(f"/sessions/{sid}/messages", params={"after_seq": 0}, headers=ana["h"])
    page = r.json()
    assert [m["kind"] for m in page["messages"]] == ["system", "system", "text"]
    assert page["messages"][-1]["client_msg_id"] == cmid and page["has_more"] is False

    r = await http.get(f"/sessions/{sid}/messages", params={"limit": 500}, headers=ana["h"])
    assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION"


async def test_non_member_gets_not_member(http: httpx.AsyncClient, settings: Settings) -> None:
    ana = await guest(http, "Ana")
    r = await http.get("/sessions/ses_other", headers=ana["h"])
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_MEMBER"
    r = await http.post(
        "/sessions/ses_other/messages",
        json={"client_msg_id": str(uuid.uuid4()), "body": "hi"},
        headers=ana["h"],
    )
    assert r.status_code == 403 and r.json()["error"]["code"] == "NOT_MEMBER"


async def test_error_body_carries_client_msg_id_and_retry_after(
    http: httpx.AsyncClient, settings: Settings
) -> None:
    ana = await guest(http, "Ana")
    url = f"/sessions/{settings.session_id}/messages"
    for i in range(10):
        body = {"client_msg_id": str(uuid.uuid4()), "body": f"m{i}"}
        assert (await http.post(url, json=body, headers=ana["h"])).status_code == 200
    cmid = str(uuid.uuid4())
    r = await http.post(url, json={"client_msg_id": cmid, "body": "x"}, headers=ana["h"])
    assert r.status_code == 429 and int(r.headers["retry-after"]) >= 1
    err = r.json()["error"]
    assert err["code"] == "RATE_LIMITED" and err["retryable"] and err["client_msg_id"] == cmid
    r = await http.post(url, json={"client_msg_id": cmid, "body": "x" * 4001}, headers=ana["h"])
    assert r.status_code == 413 and r.json()["error"]["code"] == "MESSAGE_TOO_LONG"


async def test_refresh_rotates_and_reuse_revokes_the_family(http: httpx.AsyncClient) -> None:
    ana = await guest(http, "Ana")
    r = await http.post(
        "/auth/refresh", headers={"Authorization": f"Bearer {ana['refresh_token']}"}
    )
    assert r.status_code == 200
    second = r.json()
    assert second["user_id"] == ana["user_id"] and second["refresh_token"] != ana["refresh_token"]
    r = await http.post("/auth/refresh", json={"refresh_token": second["refresh_token"]})
    assert r.status_code == 200
    third = r.json()
    # replaying a rotated token is treated as theft: the family is revoked
    r = await http.post("/auth/refresh", json={"refresh_token": ana["refresh_token"]})
    assert r.status_code == 401 and r.json()["error"]["code"] == "UNAUTHENTICATED"
    r = await http.post("/auth/refresh", json={"refresh_token": third["refresh_token"]})
    assert r.status_code == 401
    # the access token itself keeps working until it expires (15 min)
    h = {"Authorization": f"Bearer {third['access_token']}"}
    assert (await http.get("/healthz", headers=h)).status_code == 200
    r = await http.post("/auth/refresh")
    assert r.status_code == 401


async def test_guest_display_name_validation(http: httpx.AsyncClient) -> None:
    for bad in ("", "   ", "x" * 41):
        r = await http.post("/auth/guest", json={"display_name": bad})
        assert r.status_code == 400 and r.json()["error"]["code"] == "VALIDATION"


async def test_agent_get_and_patch(
    http: httpx.AsyncClient, settings: Settings, gateways: FakeGateways
) -> None:
    sid = settings.session_id
    ana = await guest(http, "Ana")
    await gateways.start("g1")
    await gateways.connect(ana["user_id"], "g1")
    r = await http.get(f"/sessions/{sid}/agent", headers=ana["h"])
    assert r.status_code == 200 and r.json()["state"] == "following"
    r = await http.patch(f"/sessions/{sid}/agent", json={"status": "paused"}, headers=ana["h"])
    assert r.status_code == 200 and r.json()["state"] == "paused"
    r = await http.patch(f"/sessions/{sid}/agent", json={"name": "Sage"}, headers=ana["h"])
    assert r.json()["name"] == "Sage"
    await gateways.drain()
    assert [e["frame"]["state"] for e in gateways.frames("g1", "agent.status")] == [
        "paused",
        "paused",
    ]


async def test_internal_api_end_to_end(http: httpx.AsyncClient, settings: Settings) -> None:
    sid = settings.session_id
    ana = await guest(http, "Ana")
    r = await http.get(f"/sessions/{sid}", headers=ana["h"])
    agent_id = r.json()["agent"]["id"]

    r = await http.get(f"/internal/sessions/{sid}/members/{ana['user_id']}", headers=SVC)
    assert r.json() == {
        "is_active": True,
        "participant_type": "user",
        "display_name": "Ana",
        "last_delivered_seq": 0,
    }
    r = await http.get(f"/internal/sessions/{sid}/members/usr_nobody", headers=SVC)
    assert r.json()["is_active"] is False

    msg = {
        "session_id": sid,
        "sender": {"type": "user", "id": ana["user_id"]},
        "client_msg_id": str(uuid.uuid4()),
        "body": "via gateway",
        "trace_id": "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01",
    }
    r = await http.post("/internal/messages", json=msg, headers=SVC)
    assert r.status_code == 200
    seq = r.json()["seq"]

    run_id = str(uuid.uuid4())
    agent_msg = {
        "session_id": sid,
        "sender": {"type": "agent", "id": agent_id},
        "client_msg_id": run_id,
        "body": "An agent thought.",
    }
    r = await http.post("/internal/messages", json=agent_msg, headers=SVC)
    assert r.status_code == 200 and r.json()["seq"] == seq + 1

    ack = {"participant_id": agent_id, "session_id": sid, "up_to_seq": seq, "kind": "read"}
    r = await http.post("/internal/acks", json=ack, headers=SVC)
    assert r.json() == {"last_delivered_seq": seq, "last_read_seq": seq, "advanced": True}

    r = await http.get(
        f"/internal/sessions/{sid}/messages", params={"after_seq": seq - 1}, headers=SVC
    )
    assert [m["seq"] for m in r.json()["messages"]] == [seq, seq + 1]

    r = await http.put(
        f"/internal/sessions/{sid}/agent/status", json={"state": "writing"}, headers=SVC
    )
    assert r.status_code == 200 and r.json()["state"] == "writing"
    r = await http.post(
        f"/internal/sessions/{sid}/agent/usage",
        json={"run_id": run_id, "tokens": 5000},
        headers=SVC,
    )
    assert r.json()["used_tokens"] == 5000 and r.json()["view"]["used_pct"] == 1
    r = await http.post(
        f"/internal/sessions/{sid}/agent/usage", json={"run_id": run_id, "tokens": -1}, headers=SVC
    )
    assert r.status_code == 400
    r = await http.get(f"/internal/sessions/{sid}/budget", headers=SVC)
    assert r.json()["used_tokens"] == 5000

    r = await http.get("/readyz")
    assert r.status_code == 200 and r.json()["postgres"] == "ok" and r.json()["redis"] == "ok"
