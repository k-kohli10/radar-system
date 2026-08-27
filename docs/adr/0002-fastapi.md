# 🚀 ADR 0002: FastAPI for All Services

## Status
Accepted

## Context
Every RADAR service is an async HTTP service exposing a small, consistent surface:
`GET /healthz`, `GET /readyz`, `GET /metrics`, and either `POST /events` (agents) or a
domain-specific API (ingestion's `/alerts/*`, llm-gateway's `/v1/complete` and
`/v1/embed`, feedback-service's Slack webhooks). All services are Python 3.12, all
database access is async (SQLAlchemy 2.0 + asyncpg), and all outbound LLM/HTTP calls
need to be non-blocking so a single service instance can handle concurrent work
without one slow call stalling everything else.

## Decision
FastAPI, on Uvicorn, for every service in `apps/`. Pydantic v2 models (from
`packages/contracts`) for all request/response validation. `opentelemetry-instrumentation-fastapi`
for automatic per-request spans.

## Consequences
- Native `async def` route handlers match the async Postgres/HTTP stack throughout the
  codebase, with no thread-pool bridging needed for I/O-bound work.
- Pydantic v2 validation gives request/response schema enforcement for free, backed by
  the same contract models used internally.
- Consistent shape across all eight services means shared middleware (agent token
  auth, correlation ID injection, OTel instrumentation) is written once in
  `packages/common` and reused everywhere, not reimplemented per service.
- OpenAPI docs are generated automatically per service, useful during local development
  even though RADAR has no external API consumers beyond Prometheus/Kibana webhooks and
  internal agent calls.
