# Build from the repo root: docker build -f services/gateway/Dockerfile .
FROM python:3.12-slim AS build
# Pinned to the uv that produced uv.lock (never :latest, so builds are reproducible).
COPY --from=ghcr.io/astral-sh/uv:0.8.17 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs/contracts libs/contracts
COPY services/gateway services/gateway
# Other workspace members only need their pyproject.toml so the workspace lockfile resolves.
COPY services/chat/pyproject.toml services/chat/pyproject.toml
COPY services/agent/pyproject.toml services/agent/pyproject.toml
# Non-editable: the venv holds real copies of huddle-gateway and huddle-contracts, so the
# runtime image needs nothing but the venv.
RUN uv sync --frozen --no-dev --no-editable --package huddle-gateway

FROM python:3.12-slim
RUN useradd --system --uid 10001 huddle
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Listen on all interfaces inside the container; the private network decides who can reach it.
ENV PATH="/app/.venv/bin:$PATH" PYTHONUNBUFFERED=1 HUDDLE_GATEWAY_HOST=0.0.0.0 HUDDLE_GATEWAY_PORT=8000
USER huddle
EXPOSE 8000
# /healthz answers 503 while draining, which marks the container unhealthy for the balancer.
HEALTHCHECK --interval=10s --timeout=3s --start-period=5s --retries=3 \
  CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"HUDDLE_GATEWAY_PORT\"]}/healthz', timeout=2)"]
# SIGTERM drains: clients get server.draining with a jittered reconnect delay (Deep Dive §2).
STOPSIGNAL SIGTERM
CMD ["python", "-m", "huddle_gateway"]
