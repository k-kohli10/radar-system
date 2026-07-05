COMPOSE_FILE := deploy/compose/docker-compose.yml
COMPOSE := docker compose --env-file .env -f $(COMPOSE_FILE)

.PHONY: setup dev stop lint test clean env-check

setup:
	uv sync
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
	uv run pytest

clean: env-check
	$(COMPOSE) down -v
