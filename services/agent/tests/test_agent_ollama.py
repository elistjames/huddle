"""OllamaModelClient against a MOCKED Ollama HTTP API (httpx.MockTransport).

These tests check the request shape (num_ctx=16384, num_predict, JSON mode, model names), the
response/usage parsing and the error mapping. They do NOT exercise a real Ollama runtime or real
Llama models; model quality and latency are unverified here.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Coroutine

import httpx
import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from huddle_agent.domain import TriggerKind
from huddle_agent.models.base import (
    DECISION_SCHEMA,
    SITUATION_SCHEMA,
    ModelError,
    ModelRequest,
    ModelTimeout,
    ModelUnavailable,
    Stage,
)
from huddle_agent.models.ollama import NUM_CTX, OllamaModelClient

Handler = Callable[[httpx.Request], Coroutine[None, None, httpx.Response]]


def ndjson(content_parts: list[str], prompt_tokens: int = 321, out_tokens: int = 12) -> bytes:
    lines: list[dict[str, object]] = [
        {
            "model": "m",
            "created_at": "2026-09-30T12:00:00Z",
            "message": {"role": "assistant", "content": part},
            "done": False,
        }
        for part in content_parts
    ]
    lines.append(
        {
            "model": "m",
            "created_at": "2026-09-30T12:00:01Z",
            "message": {"role": "assistant", "content": ""},
            "done": True,
            "done_reason": "stop",
            "prompt_eval_count": prompt_tokens,
            "eval_count": out_tokens,
        }
    )
    return b"\n".join(json.dumps(x).encode() for x in lines) + b"\n"


class FakeOllama:
    def __init__(
        self, reply: list[str] | None = None, status: int = 200, body: bytes = b""
    ) -> None:
        self.reply = reply or ["ok"]
        self.status = status
        self.body = body
        self.requests: list[dict[str, object]] = []

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": []})
        payload = json.loads(request.content)
        self.requests.append(payload)
        if self.status != 200:
            return httpx.Response(self.status, content=self.body or b'{"error":"boom"}')
        return httpx.Response(200, content=ndjson(self.reply))


def client(handler: Handler) -> OllamaModelClient:
    return OllamaModelClient(
        "http://ollama:11434", client_kwargs={"transport": httpx.MockTransport(handler)}
    )


def req(
    stage: Stage,
    model: str = "llama3.2:3b",
    max_out: int = 50,
    timeout: float = 5,
    kind: TriggerKind = TriggerKind.MESSAGE,
) -> ModelRequest:
    return ModelRequest(
        kind=kind,
        stage=stage,
        prompt=[SystemMessage(content="rules"), HumanMessage(content="hi", name="Alice")],
        model=model,
        timeout_s=timeout,
        max_output_tokens=max_out,
    )


async def test_decider_request_shape_and_parsing() -> None:
    fake = FakeOllama(['{"speak": tr', 'ue, "reason": "unanswered question"}'])
    d = await client(fake).decide(req(Stage.DECIDE))
    assert d.speak and d.reason == "unanswered question"
    assert (d.usage.input_tokens, d.usage.output_tokens) == (321, 12)
    body = fake.requests[0]
    assert body["model"] == "llama3.2:3b"
    assert body["format"] == SITUATION_SCHEMA  # a closed list of labels, not a free yes/no
    options = body["options"]
    assert isinstance(options, dict)
    assert options["num_ctx"] == NUM_CTX == 16384  # explicit: Ollama's default would truncate
    assert options["num_predict"] == 50
    assert [m["role"] for m in body["messages"]] == ["system", "user"]  # type: ignore[index,union-attr]


async def test_unparseable_decider_output_means_silence() -> None:
    d = await client(FakeOllama(["I think maybe"])).decide(req(Stage.DECIDE))
    assert not d.speak and d.reason == "unparseable decider output"


async def test_writer_uses_writer_model_and_strips_quotes() -> None:
    fake = FakeOllama(['"Friday works', ' for everyone."'])
    draft = await client(fake).write(req(Stage.WRITE, model="llama3.1:8b", max_out=300))
    assert draft.text == "Friday works for everyone."
    assert fake.requests[0]["model"] == "llama3.1:8b"
    assert "format" not in fake.requests[0] or not fake.requests[0]["format"]


@pytest.mark.parametrize(("reply", "safe"), [("safe", True), ("Unsafe\nS1", False), ("", False)])
async def test_safety_check_fails_closed(reply: str, safe: bool) -> None:
    v = await client(FakeOllama([reply])).safety_check(req(Stage.SAFETY))
    assert v.safe is safe


async def test_summarize() -> None:
    s = await client(FakeOllama(["They chose Tuesday."])).summarize(req(Stage.SUMMARIZE))
    assert s.text == "They chose Tuesday."


async def test_unreachable_runtime_is_model_unavailable() -> None:
    async def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(ModelUnavailable):
        await client(refuse).decide(req(Stage.DECIDE))
    assert await client(refuse).ping() is False


async def test_model_not_pulled_is_model_unavailable() -> None:
    fake = FakeOllama(status=404, body=b'{"error":"model \\"llama3.2:3b\\" not found"}')
    with pytest.raises(ModelUnavailable):
        await client(fake).decide(req(Stage.DECIDE))


async def test_server_error_is_transient_model_error() -> None:
    with pytest.raises(ModelError) as ei:
        await client(FakeOllama(status=500)).write(req(Stage.WRITE))
    assert ei.value.transient and not isinstance(ei.value, ModelUnavailable)


async def test_bad_request_is_not_transient() -> None:
    with pytest.raises(ModelError) as ei:
        await client(FakeOllama(status=400)).write(req(Stage.WRITE))
    assert not ei.value.transient


async def test_timeout() -> None:
    async def slow(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(2)
        return httpx.Response(200, content=ndjson(["late"]))

    with pytest.raises(ModelTimeout):
        await client(slow).decide(req(Stage.DECIDE, timeout=0.05))


async def test_ping() -> None:
    assert await client(FakeOllama()).ping() is True


async def test_llm_instances_are_cached_per_model_and_mode() -> None:
    c = client(FakeOllama())
    a = c.llm("m", 50, json_mode=True, temperature=0.0)
    assert c.llm("m", 50, json_mode=True, temperature=0.0) is a
    assert c.llm("m", 50, json_mode=False, temperature=0.0) is not a
    assert a.num_ctx == 16384


@pytest.mark.parametrize(
    ("text", "speak"),
    [
        # as seen from llama3.2:3b in the real-model eval: valid JSON cut off at the token cap,
        # or wrapped in prose
        ('{"speak": true, "reason": "Typical conference room capacity va', True),
        ('{"speak": false, "reason": "No fact-chec', False),
        ('Sure! {"speak": true, "reason": "stuck"} Let me know.', True),
        ('{\n  "speak": true,\n  "reason": "open question"', True),
        ("speak: true", True),
        ('{"speak": "yes", "reason": "x"}', True),
        ('{"speak": "no"}', False),
    ],
)
def test_parse_decision_survives_truncated_and_wrapped_answers(text: str, speak: bool) -> None:
    from huddle_agent.models.base import parse_decision

    assert parse_decision(text)[0] is speak


@pytest.mark.parametrize("text", ["", "I think so", "{}", '{"reason": "no verdict"}', "maybe"])
def test_parse_decision_without_a_verdict_stays_quiet(text: str) -> None:
    from huddle_agent.models.base import parse_decision

    assert parse_decision(text) == (False, "unparseable decider output")


def test_decision_schema_puts_speak_first() -> None:
    assert list(DECISION_SCHEMA["properties"]) == ["speak", "reason"]
    assert DECISION_SCHEMA["required"] == ["speak", "reason"]


@pytest.mark.parametrize(
    ("text", "speak", "label"),
    [
        ('{"situation": "stuck", "reason": "nobody knows"}', True, "stuck"),
        (
            '{"situation": "looks_wrong", "reason": "Sydney is not the capital"}',
            True,
            "looks_wrong",
        ),
        ('{"situation": "already_answered", "reason": "Sam answered"}', False, "already_answered"),
        ('{"situation": "small_talk", "reason": "chat"}', False, "small_talk"),
        ('{"situation": "unanswered_question", "reason": "no repl', True, "unanswered_question"),
        ('Sure: {"situation": "plans_or_status", "reason": "logistics"}', False, "plans_or_status"),
    ],
)
def test_parse_decision_turns_a_situation_label_into_a_decision(
    text: str, speak: bool, label: str
) -> None:
    from huddle_agent.models.base import parse_decision

    got, reason = parse_decision(text)
    assert got is speak and reason.startswith(label)


def test_an_unknown_label_stays_quiet() -> None:
    from huddle_agent.models.base import parse_decision

    assert parse_decision('{"situation": "maybe_speak", "reason": "x"}') == (
        False,
        "unparseable decider output",
    )


async def test_nudge_decisions_keep_the_speak_schema() -> None:
    fake = FakeOllama(['{"speak": false, "reason": "closed"}'])
    d = await client(fake).decide(req(Stage.DECIDE, kind=TriggerKind.NUDGE))
    assert d.speak is False
    assert fake.requests[0]["format"] == DECISION_SCHEMA
