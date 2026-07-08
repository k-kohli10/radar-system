COMPOSE_FILE := deploy/compose/docker-compose.yml
COMPOSE := docker compose --env-file .env -f $(COMPOSE_FILE)
SERVICES := postgres elasticsearch kibana prometheus grafana vault

.PHONY: setup dev stop lint test clean env-check svc-check start stop-one restart logs ps \
	migrate migrate-check migrate-down revision gateway gateway-check

setup:
	uv sync --all-packages
	uv run pre-commit install

env-check:
	@test -f .env || { echo "ERROR: .env not found. Run scripts/bootstrap.sh first."; exit 1; }

dev: env-check
	$(COMPOSE) up -d

stop: env-check
	$(COMPOSE) down

lint:
	uv run ruff check .
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
GATEWAY_CONFIG ?= $(GATEWAY_SECRETS_DIR)/gateway.yaml
GATEWAY_PORT ?= 8081

gateway-check:
	@test -f "$(GATEWAY_SECRETS_DIR)/openai_api_key" || { echo "ERROR: $(GATEWAY_SECRETS_DIR)/openai_api_key missing — pull it from Vault (secret/radar/llm) first."; exit 1; }
	@test -f "$(GATEWAY_SECRETS_DIR)/gateway_tokens" || { echo "ERROR: $(GATEWAY_SECRETS_DIR)/gateway_tokens missing — pull it from Vault (secret/radar/llm-gateway) first."; exit 1; }
	@test -f "$(GATEWAY_CONFIG)" || { echo "ERROR: $(GATEWAY_CONFIG) missing — write the mode config there."; exit 1; }

gateway: gateway-check
	RADAR_SECRETS_DIR="$(GATEWAY_SECRETS_DIR)" \
	RADAR_GATEWAY_CONFIG_PATH="$(GATEWAY_CONFIG)" \
	uv run uvicorn radar_llm_gateway.main:app --port $(GATEWAY_PORT) --no-access-log

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
