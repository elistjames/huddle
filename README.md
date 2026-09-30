# Huddle

People and AI in one conversation. Huddle is a real-time group chat where teammates talk to each other as usual, and an autonomous AI agent ("Huddle AI") sits in the same thread, reads the same context, and speaks up only when it has something worth adding.

**Status:** project setup. Specs and design system are in place; no application code yet.

- Product and technical specs: [`docs/`](docs/README.md)
- Design system (tokens, components, icons, logos, prototype): [`design/`](design/README.md). Open `design/prototype/huddle-prototype.html` in a browser to see the intended v1 flow.
- Instructions for coding agents: [`CLAUDE.md`](CLAUDE.md)

## Run it

```sh
docker compose -f deploy/compose.yaml up --build     # or: make up
```

Then open http://localhost:8080. Every service runs in its own container. The agent needs Ollama with two models (`llama3.2:3b`, `llama3.1:8b`); see [`deploy/README.md`](deploy/README.md) for the host and bundled options.
