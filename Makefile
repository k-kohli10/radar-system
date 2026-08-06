COMPOSE_FILE := deploy/compose/docker-compose.yml
COMPOSE := docker compose --env-file .env -f $(COMPOSE_FILE)
SERVICES := postgres elasticsearch kibana prometheus grafana vault

.PHONY: setup dev stop lint test clean env-check svc-check start stop-one restart logs ps \
	migrate migrate-check migrate-down revision gateway gateway-check gateway-secrets index \
	seed tokens rotate agent-secrets \
	dev-infra stop-infra dev-apps stop-apps apps-check ps-apps logs-apps

setup:
	uv sync --all-packages
	uv run pre-commit install

env-check:
	@test -f .env || { echo "ERROR: .env not found. Run scripts/bootstrap.sh first."; exit 1; }

# `dev`/`stop` keep their exact behaviour; `dev-infra`/`stop-infra` are clearer
# names for the same thing. They deliberately do NOT mean "infra + apps": on a
# clean machine `make dev` runs before secrets exist.
dev: env-check
	$(COMPOSE) up -d

dev-infra: dev

stop: env-check
	$(COMPOSE) down

stop-infra: stop

# Mirrors the pre-commit gate exactly. `ruff format --check` is the easy one to
# omit here, and omitting it means a green `make lint` does not predict a green
# commit — the formatter then rejects the commit over line wrapping that `ruff
# check` is perfectly happy with. Keep this list in step with
# .pre-commit-config.yaml.
lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy .

test:
	@uv run pytest; ec=$$?; if [ $$ec -eq 5 ]; then echo "no tests collected yet — ok for this phase"; exit 0; else exit $$ec; fi

# Fast inner loop: drops the `infra` Docker tests (notably the ~2min real-Prometheus
# scrape->fire->webhook proof). `make test` and CI keep them — this only spares the
# quick local loop, never the full suite.
test-quick:
	@uv run pytest -m 'not live and not infra'; ec=$$?; if [ $$ec -eq 5 ]; then echo "no tests collected yet — ok for this phase"; exit 0; else exit $$ec; fi

clean: env-check
	$(COMPOSE) down -v

# --- LLM gateway (local dev) --------------------------------------------------
# Runs the gateway against Vault-sourced secret FILES (the init-container
# pattern, simulated locally): openai_api_key + gateway_tokens pulled from
# Vault into GATEWAY_SECRETS_DIR, with the non-secret mode config beside them.
# Override any of these per invocation, e.g. `make gateway GATEWAY_PORT=9000`.
GATEWAY_SECRETS_DIR ?= $(HOME)/.radar-dev/secrets
GATEWAY_CONFIG ?= apps/llm-gateway/config/gateway.yaml
GATEWAY_PORT ?= 8081

gateway-check:
	@test -f "$(GATEWAY_SECRETS_DIR)/openai_api_key" || { echo "ERROR: $(GATEWAY_SECRETS_DIR)/openai_api_key missing — pull it from Vault (secret/radar/llm) first."; exit 1; }
	@test -f "$(GATEWAY_SECRETS_DIR)/gateway_tokens" || { echo "ERROR: $(GATEWAY_SECRETS_DIR)/gateway_tokens missing — pull it from Vault (secret/radar/llm-gateway) first."; exit 1; }
	@test -f "$(GATEWAY_CONFIG)" || { echo "ERROR: $(GATEWAY_CONFIG) missing — write the mode config there."; exit 1; }

gateway: gateway-check
	RADAR_SECRETS_DIR="$(GATEWAY_SECRETS_DIR)" \
	RADAR_GATEWAY_CONFIG_PATH="$(GATEWAY_CONFIG)" \
	uv run uvicorn radar_llm_gateway.main:app --port $(GATEWAY_PORT) --no-access-log

# Re-pull gateway secrets from the dev Vault into GATEWAY_SECRETS_DIR (the
# local init-container simulation). Run after changing values in Vault, then
# restart the gateway.
gateway-secrets: env-check
	RADAR_SECRETS_DIR="$(GATEWAY_SECRETS_DIR)" uv run python scripts/dev-gateway-secrets.py

# --- Ingestion (local dev) ---------------------------------------------------
# Re-pull ingestion's secrets from the dev Vault into ITS OWN directory,
# $(INGESTION_SECRETS_DIR)/ingestion/ — webhook tokens one file PER SOURCE
# (independent rotation, ADR 0011) plus postgres_dsn. Nothing else assembles
# this directory (ingestion has no agent_token, so `make agent-secrets` skips
# it). Run after changing a token in Vault, then restart ingestion with
# RADAR_SECRETS_DIR pointed at that subdirectory.
INGESTION_SECRETS_DIR ?= $(HOME)/.radar-dev/secrets

ingestion-secrets: env-check
	RADAR_SECRETS_DIR="$(INGESTION_SECRETS_DIR)" uv run python scripts/dev-ingestion-secrets.py

# --- Internal tokens (local dev) ---------------------------------------------
# Two halves, and the split matters: `seed` restores what a HUMAN supplies (the
# DSN, the provider key, from .env); `tokens` mints what the PLATFORM generates
# (per-service agent tokens, the worker's dispatch map, gateway mode grants,
# webhook tokens). Together they rebuild a dev Vault from empty — which matters,
# because the dev Vault is in-memory and loses everything when its container
# restarts.
#
#   make seed && make tokens && make agent-secrets     # from scratch
#
# `tokens` is idempotent (existing tokens are KEPT, never clobbered) and
# convergent (the worker's dispatch_tokens map is rebuilt from the per-service
# tokens every run, so it cannot drift from them). Safe to re-run any time.
AGENT_SECRETS_DIR ?= $(HOME)/.radar-dev/secrets

seed: env-check
	uv run python scripts/dev-seed-vault.py

tokens: env-check
	uv run python scripts/dev-mint-tokens.py

# Rotate ONE service's credentials: a fresh agent token, a fresh gateway token if
# it has one, and — the second write, without which the first is useless — the
# outbox worker's dispatch_tokens entry pointing at it.
#
# NOT a hot operation. Between the two pods restarting, the worker sends the old
# token and the target rejects it; a 401 is classified permanent, so those events
# are dead-lettered rather than retried. Rotate on a drained pipeline: check
# outbox depth first. See the carried-debt note in docs/roadmap.md.
#
#   make rotate SERVICE=reasoner-agent
rotate: env-check
	@test -n "$(SERVICE)" || { echo 'usage: make rotate SERVICE=<service>'; exit 1; }
	uv run python scripts/dev-mint-tokens.py --rotate $(SERVICE)
	@echo "--> now: make agent-secrets, then restart $(SERVICE) AND outbox-worker"

# Pull each agent's secrets into its OWN directory ($(AGENT_SECRETS_DIR)/<service>/).
# One directory per service, not one shared: every service reads its token from a
# file with a fixed name, so a shared directory would mean a shared token — which
# is exactly what per-service tokens exist to prevent.
agent-secrets: env-check
	RADAR_SECRETS_DIR="$(AGENT_SECRETS_DIR)" uv run python scripts/dev-agent-secrets.py

# --- Runbook indexing ---------------------------------------------------------
# One incremental pass over docs/runbooks into Elasticsearch. Needs the
# knowledge-service secrets (make tokens && make agent-secrets) and a running
# gateway (make gateway). Re-running on an unchanged corpus is a no-op.
KNOWLEDGE_SECRETS_DIR ?= $(HOME)/.radar-dev/secrets/knowledge-service

index:
	@test -f "$(KNOWLEDGE_SECRETS_DIR)/gateway_token_embed" || { echo "ERROR: $(KNOWLEDGE_SECRETS_DIR)/gateway_token_embed missing — run 'make tokens && make agent-secrets' first."; exit 1; }
	RADAR_SECRETS_DIR="$(KNOWLEDGE_SECRETS_DIR)" uv run python -m radar_knowledge_service.index

# --- The application processes (native, not containers) ----------------------
# Native because this is the code you edit; containerising is Phase 11/12.
# Stopping is by PID FILE, never `pkill -f uvicorn` — a pattern kill would take
# out uvicorn processes belonging to other projects on the same machine.
SECRETS_ROOT ?= $(HOME)/.radar-dev/secrets
RUN_DIR      := .dev-run
GATEWAY_URL  := http://127.0.0.1:8081
KNOWLEDGE_URL:= http://127.0.0.1:8095
DISPATCH_OVERRIDES := {"watcher-agent":"http://127.0.0.1:8091/events","planner-agent":"http://127.0.0.1:8092/events","reasoner-agent":"http://127.0.0.1:8093/events","feedback-service":"http://127.0.0.1:8096/events"}

#: name:port:mode:module — NO SPACES inside an entry: make word-splits this
#: list, so a `--factory ` would be read as two separate apps. `mode` is
#: `factory` where the service exposes create_app.
APPS := \
	llm-gateway:8081:app:radar_llm_gateway.main:app \
	knowledge-service:8095:app:radar_knowledge_service.main:app \
	ingestion:8090:factory:radar_ingestion.main:create_app \
	watcher-agent:8091:factory:radar_watcher_agent.main:create_app \
	planner-agent:8092:factory:radar_planner_agent.main:create_app \
	reasoner-agent:8093:factory:radar_reasoner_agent.main:create_app \
	outbox-worker:8094:factory:radar_outbox_worker.main:create_app \
	feedback-service:8096:factory:radar_feedback_service.main:create_app

apps-check:
	@test -d "$(SECRETS_ROOT)" || { echo "ERROR: $(SECRETS_ROOT) missing — run 'make seed && make tokens && make agent-secrets && make gateway-secrets && make ingestion-secrets' first."; exit 1; }
	@test -f "$(SECRETS_ROOT)/gateway_tokens" || { echo "ERROR: $(SECRETS_ROOT)/gateway_tokens missing — run 'make gateway-secrets'."; exit 1; }

dev-apps: apps-check
	@mkdir -p $(RUN_DIR)
	@for entry in $(APPS); do \
		name=$${entry%%:*}; rest=$${entry#*:}; port=$${rest%%:*}; \
		rest=$${rest#*:}; mode=$${rest%%:*}; module=$${rest#*:}; \
		if [ -f $(RUN_DIR)/$$name.pid ] && kill -0 $$(cat $(RUN_DIR)/$$name.pid) 2>/dev/null; then \
			echo "  $$name already running (pid $$(cat $(RUN_DIR)/$$name.pid))"; continue; fi; \
		if [ "$$name" = "llm-gateway" ]; then secrets="$(SECRETS_ROOT)"; \
		else secrets="$(SECRETS_ROOT)/$$name"; fi; \
		if [ "$$mode" = "factory" ]; then factory="--factory"; else factory=""; fi; \
		RADAR_SECRETS_DIR="$$secrets" \
		RADAR_GATEWAY_CONFIG_PATH="$(GATEWAY_CONFIG)" \
		RADAR_GATEWAY_URL="$(GATEWAY_URL)" \
		RADAR_KNOWLEDGE_URL="$(KNOWLEDGE_URL)" \
		RADAR_DISPATCH_URL_OVERRIDES='$(DISPATCH_OVERRIDES)' \
		nohup uv run uvicorn $$factory $$module --port $$port --no-access-log \
			> $(RUN_DIR)/$$name.log 2>&1 & \
		echo $$! > $(RUN_DIR)/$$name.pid; \
		echo "  started $$name on :$$port (pid $$!)"; \
	done
	@echo "\nwaiting for readiness..."
	@sleep 4
	@$(MAKE) --no-print-directory ps-apps

# Polled and printed: a backgrounded process that died on a missing secret must
# say so here, not be discovered when an alert vanishes.
ps-apps:
	@for entry in $(APPS); do \
		name=$${entry%%:*}; rest=$${entry#*:}; port=$${rest%%:*}; \
		code=$$(curl -s -o /dev/null -w "%{http_code}" --max-time 3 http://127.0.0.1:$$port/readyz 2>/dev/null); \
		case $$code in \
			200) state="ready";; \
			000) state="DOWN — see $(RUN_DIR)/$$name.log";; \
			*)   state="not ready (HTTP $$code): $$(curl -s --max-time 3 http://127.0.0.1:$$port/readyz | head -c 90)";; \
		esac; \
		printf "  %-20s :%s  %s\n" "$$name" "$$port" "$$state"; \
	done

logs-apps:
	@tail -n 40 -f $(RUN_DIR)/*.log

stop-apps:
	@if [ ! -d $(RUN_DIR) ]; then echo "  nothing to stop"; exit 0; fi
	@for pidfile in $(RUN_DIR)/*.pid; do \
		[ -e "$$pidfile" ] || continue; \
		name=$$(basename $$pidfile .pid); pid=$$(cat $$pidfile); \
		if kill -0 $$pid 2>/dev/null; then \
			pkill -P $$pid 2>/dev/null || true; kill $$pid 2>/dev/null || true; \
			echo "  stopped $$name (pid $$pid)"; \
		else echo "  $$name was not running"; fi; \
		rm -f $$pidfile; \
	done

# --- Database migrations (Alembic) -------------------------------------------
# alembic.ini lives in packages/database and env.py reads POSTGRES_DSN, which we
# load from the repo-root .env (../../.env once inside the package directory).
MIGRATE := cd packages/database && POSTGRES_DSN="$$(grep '^POSTGRES_DSN=' ../../.env | cut -d= -f2-)" uv run alembic

migrate: env-check
	$(MIGRATE) upgrade head

migrate-check: env-check
	$(MIGRATE) check

migrate-down: env-check
	$(MIGRATE) downgrade -1

revision: env-check
	@test -n "$(m)" || { echo 'usage: make revision m="message"'; exit 1; }
	$(MIGRATE) revision --autogenerate -m "$(m)"

# --- Single-service controls: make <target> s=<service> ----------------------
# Services: $(SERVICES)

svc-check: env-check
	@test -n "$(s)" || { echo "usage: make $(MAKECMDGOALS) s=<service>"; echo "services: $(SERVICES)"; exit 1; }

start: svc-check
	$(COMPOSE) up -d $(s)

stop-one: svc-check
	$(COMPOSE) stop $(s)

restart: svc-check
	$(COMPOSE) restart $(s)

logs: env-check
	$(COMPOSE) logs -f $(s)

ps: env-check
	$(COMPOSE) ps
