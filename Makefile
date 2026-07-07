COMPOSE_FILE := deploy/compose/docker-compose.yml
COMPOSE := docker compose --env-file .env -f $(COMPOSE_FILE)
SERVICES := postgres elasticsearch kibana prometheus grafana vault

.PHONY: setup dev stop lint test clean env-check svc-check start stop-one restart logs ps \
	migrate migrate-check migrate-down revision

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
