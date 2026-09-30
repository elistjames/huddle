# Running everything in containers

One command starts Postgres, Redis, the chat service, the gateway, the agent and the web app, each in
its own container on a private network (standing in for separate servers).

```sh
docker compose -f deploy/compose.yaml up --build     # or: make up
```

Open **http://localhost:8080**. Join in two private windows to be two people.

## Ollama (the agent's models)

The agent needs Ollama with `llama3.2:3b` (decider) and `llama3.1:8b` (writer). Pick one:

- **Ollama on your machine (default).** The agent reaches it at `host.docker.internal:11434`. Ollama
  must listen on all interfaces, not just loopback, or the container cannot connect:
  `OLLAMA_HOST=0.0.0.0 ollama serve` (systemd: set it in the service override, then restart).
  On WSL with Docker Desktop, run Ollama inside WSL with that setting, or use the bundled one.
- **Bundled Ollama in Compose.** Put `HUDDLE_OLLAMA_URL=http://ollama:11434` in `deploy/.env`, then
  `docker compose -f deploy/compose.yaml --profile ollama up --build` and pull the models once:
  `make pull-models`.

Without Ollama the app still works as a plain chat and the header shows "Huddle AI is away right now".

## Configuration

Defaults are for local development only (known passwords and secrets). Copy `deploy/.env.example` to
`deploy/.env` to override anything; it is read automatically if present. Never deploy with the defaults.

## Handy commands (from the repo root)

| Command | What it does |
|---|---|
| `make up` | build and start everything in the foreground |
| `make upd` | same, in the background |
| `make logs` | follow all logs (`make logs s=agent` for one service) |
| `make down` | stop (keeps the database volume); `make nuke` also deletes volumes |
| `make pull-models` | pull the two models into the bundled Ollama |
| `make ps` | container status and health |

## What is exposed

Only the web app (`8080`) is meant to be used directly. Chat (`8001`), gateway (`8000`), Postgres and
Redis are published on `127.0.0.1` for debugging only. The web container proxies `/auth`, `/sessions`
to chat and `/ws` to the gateway (`apps/web/nginx.conf.template`), so the browser sees one origin.

## Scaling notes

Any gateway can take any connection (no sticky sessions), so `docker compose up --scale gateway=3` works
if you put a load balancer in front and remove the fixed `gw-1` id (set `HUDDLE_GATEWAY_ID` per instance).
Run more than one agent worker with `--scale agent=2`; the run lease keeps one live run per session.
