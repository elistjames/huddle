# Build from the repo root: docker build -f services/agent/Dockerfile .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
# Use the image's interpreter (the runtime stage has the same one); never download another.
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs/contracts libs/contracts
COPY services/agent services/agent
# Workspace members other than this service and contracts are stubs so the lockfile resolves.
COPY services/gateway/pyproject.toml services/gateway/pyproject.toml
COPY services/chat/pyproject.toml services/chat/pyproject.toml
# Non-editable: the runtime image gets installed wheels only (no sources or tests).
RUN uv sync --frozen --no-dev --no-editable --package huddle-agent

FROM python:3.12-slim
RUN useradd --system --uid 10001 huddle
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# /healthz must be reachable by the container runtime's probe; the worker itself serves nothing
# else. Ollama runs on the host (deploy/compose.yaml maps host.docker.internal).
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 \
    HUDDLE_AGENT_HEALTH_HOST=0.0.0.0 HUDDLE_AGENT_HEALTH_PORT=8002
USER huddle
EXPOSE 8002
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8002/healthz', timeout=2)"]
# Consumes agent:triggers, runs the speak policy and commits through the chat service.
CMD ["python", "-m", "huddle_agent"]
