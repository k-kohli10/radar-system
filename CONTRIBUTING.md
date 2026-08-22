# Contributing to RADAR

RADAR is built phase by phase against
[docs/implementation_plan.md](docs/implementation_plan.md). That document is the source
of truth for scope, locked decisions, and per-phase deliverables. Read it before opening
a PR.

## Ground Rules

- **One phase, one PR.** Each phase in the implementation plan has a defined milestone
  and a defined set of deliverables. Do not mix work from two phases in one PR, and do
  not build ahead of the current phase.
- **Locked decisions are locked.** The "Locked Decisions" section of the implementation
  plan (agent frameworks, notification channel, secrets handling, etc.) is not up for
  revisiting mid-implementation. Propose changes via a new ADR, not a silent deviation.
- **No dump commits.** Commit history should read as a narrative of how the system grew.
  Keep commits small, scoped, and imperative mood (`feat(scope): ...`,
  `test(scope): ...`, `docs: ...`) instead of "add everything" commits.
- **Config, not code, for anything domain-tunable.** Correlation rules and plan templates
  are YAML, mounted as ConfigMaps. Don't hardcode what the plan says is config.

## Development Setup

See [docs/implementation_plan.md](docs/implementation_plan.md), Phase 1, for the full
local environment spec (`make setup`, `make dev-infra-up`, Docker Compose stack, `.env.example`).

## Code Standards

- Python 3.12+, `uv` for dependency management.
- `ruff check .` and `mypy .` must pass (`make lint`).
- `pytest` must pass (`make test`).
- Every service exposes `GET /healthz`, `GET /readyz`, `GET /metrics`, structured JSON
  logs via `structlog` with a `correlation_id` on every line, and an OTel span per
  request. See "Every Service Must Have" in the implementation plan.
- Every outbound HTTP call has a timeout and bounded retries.

## Testing Expectations

New logic ships with tests in the same PR. Agent logic in particular must cover the
three invariants called out in the plan: outbox atomicity (no incident without its
outbox event, or vice versa), concurrent poller isolation (no double-processing), and
idempotency (replays of the same event are no-ops).

## Pull Requests

- Reference the phase and milestone the PR completes.
- Use the PR template in `.github/PULL_REQUEST_TEMPLATE.md`.
- CI (lint, typecheck, test, multi-arch build) must pass before merge.

## Reporting Issues

Use the templates in `.github/ISSUE_TEMPLATE/`.
