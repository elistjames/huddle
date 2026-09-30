from __future__ import annotations

import pytest
from agent_testkit import make_executor

from huddle_agent.clock import FakeClock
from huddle_agent.config import Settings
from huddle_agent.memory import MemoryWorld
from huddle_agent.models.fake import ScriptedModel
from huddle_agent.runner import RunExecutor


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def world(clock: FakeClock) -> MemoryWorld:
    return MemoryWorld(clock=clock)


@pytest.fixture
def model(clock: FakeClock) -> ScriptedModel:
    return ScriptedModel(clock=clock)


@pytest.fixture
def settings() -> Settings:
    return Settings(worker_id="worker-1")


@pytest.fixture
def executor(world: MemoryWorld, model: ScriptedModel, settings: Settings) -> RunExecutor:
    return make_executor(world, model, settings)
