# 🧰 radar-common

Shared runtime primitives for every RADAR service.

| Module | Provides |
|---|---|
| `logging` | structlog JSON to stdout with `correlation_id` bound on every line |
| `config` | Settings loader with Vault secret-file support. No secrets from env |
| `auth` | `X-Radar-Agent-Token` FastAPI dependency |
| `errors` | The RADAR error hierarchy |
| `ids` | UUID helpers for event and correlation ids |
| `time` | Timezone-aware UTC helpers |

Depends only on `fastapi`, `pydantic`, `pydantic-settings`, and `structlog`.
mypy strict must pass.
