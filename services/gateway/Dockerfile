# Build from the repo root: docker build -f services/gateway/Dockerfile .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs/contracts libs/contracts
COPY services/gateway services/gateway
# Workspace members other than this service and contracts are stubs so the lockfile resolves.
COPY services/gateway/pyproject.toml services/gateway/pyproject.toml
COPY services/chat/pyproject.toml services/chat/pyproject.toml
COPY services/agent/pyproject.toml services/agent/pyproject.toml
RUN uv sync --frozen --no-dev --package huddle-gateway

FROM python:3.12-slim
RUN useradd --system --uid 10001 huddle
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER huddle
CMD ["python", "-m", "huddle_gateway"]
