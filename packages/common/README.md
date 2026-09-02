# 🧰 radar-common

Shared runtime primitives for every RADAR service:

- **logging**: structlog JSON to stdout with `correlation_id` bound on every line
- **config**: settings loader with Vault secret-file support (no secrets from env)
- **auth**: `X-Radar-Agent-Token` FastAPI dependency
- **errors**: the RADAR error hierarchy
- **ids**: UUID helpers for event and correlation ids
- **time**: timezone-aware UTC helpers

Depends only on `fastapi`, `pydantic`, `pydantic-settings`, and `structlog`.
mypy strict must pass.
