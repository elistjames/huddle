"""Ollama model client via LangChain's `ChatOllama` (Deep Dive §5, D-015).

NOTE: this client is unit-tested against a mocked Ollama HTTP transport only. It has not been
exercised against a real Ollama runtime or real Llama models in this repository's CI.

* `num_ctx` is set explicitly to 16k: Ollama's default is much smaller and would silently cut
  off the prompt.
* An unreachable runtime (connection refused, or the model is not pulled) raises
  `ModelUnavailable`, which the orchestrator surfaces as the `away` state.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from langchain_core.messages import BaseMessage
from langchain_ollama import ChatOllama
from ollama import ResponseError

from huddle_agent.context import estimate_tokens
from huddle_agent.models.base import (
    DECISION_SCHEMA,
    Decision,
    Draft,
    ModelError,
    ModelRequest,
    ModelTimeout,
    ModelUnavailable,
    SafetyVerdict,
    SummaryText,
    Usage,
    parse_decision,
    parse_safety,
)

NUM_CTX = 16_384


class OllamaModelClient:
    def __init__(
        self,
        base_url: str,
        *,
        num_ctx: int = NUM_CTX,
        client_kwargs: dict[str, Any] | None = None,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._num_ctx = num_ctx
        # Extra httpx.AsyncClient kwargs (tests inject a mocked `transport` here).
        self._client_kwargs: dict[str, Any] = dict(client_kwargs or {})
        self._llms: dict[tuple[str, int, bool, float, str], ChatOllama] = {}

    def llm(
        self,
        model: str,
        max_tokens: int,
        *,
        json_mode: bool,
        temperature: float,
        schema: dict[str, Any] | None = None,
    ) -> ChatOllama:
        key = (model, max_tokens, json_mode, temperature, json.dumps(schema, sort_keys=True))
        llm = self._llms.get(key)
        if llm is None:
            llm = ChatOllama(
                model=model,
                base_url=self._base_url,
                num_ctx=self._num_ctx,
                num_predict=max_tokens,
                temperature=temperature,
                format=schema if schema is not None else ("json" if json_mode else None),
                async_client_kwargs=self._client_kwargs,
                validate_model_on_init=False,
            )
            self._llms[key] = llm
        return llm

    async def _invoke(
        self,
        req: ModelRequest,
        *,
        json_mode: bool,
        temperature: float,
        schema: dict[str, Any] | None = None,
    ) -> tuple[str, Usage]:
        llm = self.llm(
            req.model,
            req.max_output_tokens,
            json_mode=json_mode,
            temperature=temperature,
            schema=schema,
        )
        prompt: list[BaseMessage] = list(req.prompt)
        try:
            msg = await asyncio.wait_for(llm.ainvoke(prompt), timeout=req.timeout_s)
        except TimeoutError as exc:
            raise ModelTimeout(f"{req.stage} timed out after {req.timeout_s}s") from exc
        except (ConnectionError, httpx.ConnectError, httpx.ConnectTimeout) as exc:
            raise ModelUnavailable("model runtime unreachable") from exc
        except ResponseError as exc:
            if exc.status_code == 404:  # model not pulled: same operator fix as a down runtime
                raise ModelUnavailable(f"model {req.model} not available") from exc
            raise ModelError(
                f"ollama error {exc.status_code}", transient=exc.status_code >= 500
            ) from exc
        except httpx.HTTPError as exc:
            raise ModelError(type(exc).__name__, transient=True) from exc
        text = str(msg.text)
        meta = msg.usage_metadata
        if meta is not None:
            usage = Usage(req.model, int(meta["input_tokens"]), int(meta["output_tokens"]))
        else:  # the runtime did not report counts; fall back to estimates
            est_in = sum(estimate_tokens(str(m.content)) for m in prompt)
            usage = Usage(req.model, est_in, estimate_tokens(text))
        return text, usage

    async def decide(self, req: ModelRequest) -> Decision:
        text, usage = await self._invoke(
            req, json_mode=True, temperature=0.0, schema=DECISION_SCHEMA
        )
        speak, reason = parse_decision(text)
        return Decision(speak=speak, reason=reason, usage=usage, raw=text)

    async def write(self, req: ModelRequest) -> Draft:
        text, usage = await self._invoke(req, json_mode=False, temperature=0.7)
        return Draft(text=text.strip().strip('"').strip(), usage=usage)

    async def summarize(self, req: ModelRequest) -> SummaryText:
        text, usage = await self._invoke(req, json_mode=False, temperature=0.2)
        return SummaryText(text=text.strip(), usage=usage)

    async def safety_check(self, req: ModelRequest) -> SafetyVerdict:
        text, usage = await self._invoke(req, json_mode=False, temperature=0.0)
        return SafetyVerdict(safe=parse_safety(text), usage=usage)

    async def ping(self) -> bool:
        try:
            async with httpx.AsyncClient(
                base_url=self._base_url, timeout=3.0, **self._client_kwargs
            ) as client:
                resp = await client.get("/api/tags")
                return resp.status_code == 200
        except httpx.HTTPError:
            return False
