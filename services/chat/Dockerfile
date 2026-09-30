# Build from the repo root: docker build -f services/chat/Dockerfile .
FROM python:3.12-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
# Use the image's interpreter (the runtime stage has the same one); never download another.
ENV UV_PYTHON_DOWNLOADS=never UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs/contracts libs/contracts
COPY services/chat services/chat
# Workspace members other than this service and contracts are stubs so the lockfile resolves.
COPY services/gateway/pyproject.toml services/gateway/pyproject.toml
COPY services/agent/pyproject.toml services/agent/pyproject.toml
RUN uv sync --frozen --no-dev --package huddle-chat

FROM python:3.12-slim
RUN useradd --system --uid 10001 huddle
WORKDIR /app
COPY --from=build /app /app
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1
USER huddle
EXPOSE 8001
HEALTHCHECK --interval=10s --timeout=3s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8001/healthz', timeout=2)"]
# Migrates the schema (advisory-locked) and bootstraps the MVP session, then serves on 8001.
CMD ["python", "-m", "huddle_chat"]
