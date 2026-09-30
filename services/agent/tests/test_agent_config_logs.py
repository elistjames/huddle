"""Settings, policy config, structured logs (Deep Dive §9) and the health endpoint."""

from __future__ import annotations

import json
import logging

import httpx
import pytest
from agent_testkit import make_executor

from huddle_agent import logs
from huddle_agent.config import PolicyConfig, Settings
from huddle_agent.health import create_health_app
from huddle_agent.memory import MemoryWorld
from huddle_agent.models.base import Stage
from huddle_agent.models.fake import ScriptedModel
from huddle_contracts import redis_keys


def test_settings_from_env() -> None:
    s = Settings.from_env(
        {
            "HUDDLE_POSTGRES_DSN": "postgresql://u@db/x",
            "HUDDLE_REDIS_URL": "redis://r:6379/1",
            "HUDDLE_SERVICE_TOKEN": "tok",
            "HUDDLE_CHAT_URL": "http://chat:8001",
            "HUDDLE_OLLAMA_URL": "http://ollama:11434",
            "HUDDLE_DECIDER_MODEL": "llama3.2:3b",
            "HUDDLE_WRITER_MODEL": "llama3.1:8b",
            "HUDDLE_SESSION_ID": "ses_x",
            "HUDDLE_AGENTS_DISABLED": "true",
            "HUDDLE_AGENT_HEALTH_PORT": "9000",
        }
    )
    assert s.postgres_dsn == "postgresql://u@db/x" and s.chat_url == "http://chat:8001"
    assert (s.decider_model, s.writer_model) == ("llama3.2:3b", "llama3.1:8b")
    assert s.agents_disabled and s.health_port == 9000 and s.session_id == "ses_x"
    assert not Settings.from_env({}).agents_disabled
    assert Settings.from_env({}).session_id == "ses_main"


def test_redis_names_come_from_contracts() -> None:
    s = Settings()
    assert s.triggers_stream == redis_keys.AGENT_TRIGGERS_STREAM == "agent:triggers"
    assert s.triggers_group == redis_keys.AGENT_TRIGGERS_GROUP == "agent-workers"
    assert s.timers_zset == redis_keys.AGENT_TIMERS_ZSET == "agent:timers"


def test_policy_config_defaults_match_spec() -> None:
    p = PolicyConfig()
    assert (p.debounce_quiet_s, p.debounce_cap_s, p.cooldown_s, p.cooldown_max_s) == (3, 8, 20, 300)
    assert (p.share_window, p.share_cap_factor, p.share_nudge_factor) == (30, 1.25, 0.5)
    assert (p.nudge_delay_s, p.max_chars, p.target_chars) == (1200, 1000, 600)
    assert (p.decider_timeout_s, p.writer_timeout_s) == (10, 45)
    assert (p.recent_window_tokens, p.fold_threshold_tokens, p.fold_fraction) == (6000, 12000, 0.8)


def test_policy_config_from_agents_config_is_defensive() -> None:
    p = PolicyConfig.from_json(
        {"cooldown_s": 30, "share_window": "40", "nudge_enabled": 0, "bogus": 1, "max_chars": 800}
    )
    assert p.cooldown_s == 30.0 and p.max_chars == 800
    assert p.share_window == 30 and p.nudge_enabled is True  # badly typed values ignored


def test_json_log_has_required_fields(capsys: pytest.CaptureFixture[str]) -> None:
    logs.configure()
    logs.log_event("agent_run_started", session_id="ses_main", seq=3, trace_id="00-t")
    line = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    for name in logs.REQUIRED_FIELDS:
        assert name in line
    assert line["service"] == "agent" and line["event"] == "agent_run_started"


def test_log_event_refuses_content_fields() -> None:
    with pytest.raises(ValueError, match="content"):
        logs.log_event("oops", body="secret")


async def test_runs_never_log_message_bodies(
    world: MemoryWorld, caplog: pytest.LogCaptureFixture
) -> None:
    logs.configure()
    logs.logger.propagate = True
    try:
        secret_in = "my password is hunter2, what now?"
        secret_out = "Please rotate hunter2 today."
        world.post_human("usr_alice", secret_in)
        model = ScriptedModel()
        model.script(Stage.DECIDE, True).script(Stage.WRITE, secret_out)
        with caplog.at_level(logging.INFO, logger=logs.LOGGER_NAME):
            await make_executor(world, model).execute(world.session_id)
        formatter = logs.JsonFormatter()
        rendered = "\n".join(formatter.format(r) for r in caplog.records)
        assert "agent_run_observed" in rendered
        assert "hunter2" not in rendered
    finally:
        logs.logger.propagate = False


async def test_health_endpoint() -> None:
    async def up() -> bool:
        return True

    async def down() -> bool:
        return False

    ok = create_health_app(lambda: True, {"redis": up, "postgres": up}, disabled=False)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=ok), base_url="http://a") as c:
        r = await c.get("/healthz")
    assert r.status_code == 200 and r.json()["status"] == "ok"

    bad = create_health_app(lambda: True, {"redis": up, "postgres": down}, disabled=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=bad), base_url="http://a") as c:
        r = await c.get("/healthz")
    assert r.status_code == 503 and r.json()["postgres"] is False
    assert r.json()["agents_disabled"] is True
