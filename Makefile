COMPOSE_FILE := deploy/compose/docker-compose.yml
COMPOSE := docker compose --env-file .env -f $(COMPOSE_FILE)
SERVICES := postgres elasticsearch kibana prometheus grafana vault

.PHONY: setup dev stop lint test clean env-check svc-check start stop-one restart logs ps \
	migrate migrate-check migrate-down revision gateway gateway-check gateway-secrets \
	seed tokens rotate agent-secrets

setup:
	uv sync --all-packages
	uv run pre-commit install

env-check:
	@test -f .env || { echo "ERROR: .env not found. Run scripts/bootstrap.sh first."; exit 1; }

dev: env-check
	$(COMPOSE) up -d

stop: env-check
	$(COMPOSE) down

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
