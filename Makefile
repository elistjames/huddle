COMPOSE = docker compose -f deploy/compose.yaml
s ?=

.PHONY: up upd down nuke logs ps pull-models gate eval-decider

up:            ## build and start everything (foreground)
	$(COMPOSE) up --build

upd:           ## build and start everything (background)
	$(COMPOSE) up --build -d

down:          ## stop, keep volumes
	$(COMPOSE) down

nuke:          ## stop and delete volumes (database and models)
	$(COMPOSE) --profile ollama down -v

logs:          ## follow logs, optionally one service: make logs s=agent
	$(COMPOSE) logs -f $(s)

ps:
	$(COMPOSE) ps

pull-models:   ## pull the agent's models into the bundled Ollama
	$(COMPOSE) --profile ollama up -d ollama
	$(COMPOSE) exec ollama ollama pull llama3.2:3b
	$(COMPOSE) exec ollama ollama pull llama3.1:8b

gate:          ## the merge gate
	./scripts/gate.sh

eval-decider:  ## real-model decider eval against your Ollama (not CI); args: make eval-decider a="--only stuck -v"
	uv run python -m huddle_agent.evals.decider $(a)
