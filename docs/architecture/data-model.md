# Data Model

All state lives in Postgres. There is no separate ticketing system and no Redis —
Postgres is both the system of record and the message backbone (via the outbox tables).

## Schema Conventions

```
- UUIDs for all primary keys, generated application-side
- All timestamps are TIMESTAMPTZ
- JSONB for flexible payloads only — never for a column you filter on
- Every foreign key has an index
- Every WHERE-clause column has an index
- Partial indexes on hot paths (the outbox poller)
- audit_log is append-only: never updated, never deleted
```

## Entities

### alerts
Raw alert instances from Prometheus or Kibana, normalized on ingest. Linked to the
incident they were correlated into via `incident_id`. Carries `fingerprint` (used for
deduplication) and `correlation_id` (used for tracing a single incident's lifecycle
across every table and every log line).

### incidents
The unit of work RADAR reasons about. One incident aggregates one or more alerts that
share a fingerprint within a correlation window. `status` moves through
`open → resolved → closed`. `alert_count` increments as duplicate/related alerts arrive.

### investigation_plans
Exactly one plan per incident (enforced by a unique index on `incident_id`). `steps` is
a JSONB array of ordered, human-readable investigation actions, produced by matching
`template_key` against the planner's YAML templates.

### recommendations
The reasoner's output. Captures which provider/model produced it
(`llm_provider`, `model_alias`, `model_id`), the root cause narrative, a confidence
level, the recommended actions, the context bundle it reasoned over, and token/latency
usage. `is_fallback=true` marks a template-generated RCA produced when the LLM was
unavailable — the incident still gets a recommendation, just not an AI-derived one.

### feedback
Engineer reactions to a recommendation, captured from Slack (👍/👎/correction).
Linked to both the `recommendation` and the parent `incident`. `slack_user_id` and
`slack_message_ts` tie it back to the exact Slack interaction.

### outbox_events
The transactional outbox. Every cross-agent handoff is a row here, written in the same
transaction as the state change that produced it. `status` moves
`pending → processing → delivered` or `dead_letter`. `process_after` drives retry
scheduling.

### processed_events
Idempotency ledger. One row per `(event_id, processed_by)` pair. Checked before an
agent does any work for an inbound event, written in the same transaction as that
work, so a redelivered event is a no-op on replay.

### runbook_documents
Index manifest for knowledge-service (Phase 8+). Tracks which runbook files have been
chunked and embedded, keyed by `content_hash` so re-indexing only happens when a
runbook's content actually changes.

### audit_log
Append-only trail of significant state transitions across all entities, keyed by
`entity_type` + `entity_id`, always carrying `correlation_id`. Never updated or
deleted — it is the record of what RADAR did and when, independent of what the
mutable tables currently say.

## The correlation_id Thread

Every alert, incident, plan, recommendation, feedback row, outbox event, log line, and
OTel span carries the same `correlation_id` for a given incident's lifecycle. This is
what makes it possible to reconstruct — in Kibana APM, in Postgres, or in raw logs —
the complete path from "an alert fired" to "an engineer clicked 👍," using nothing but
that one ID.

## Full DDL

The authoritative schema (all tables, columns, and indexes) is defined in
[docs/implementation_plan.md](../implementation_plan.md) under "Postgres Schema." That
DDL is what Alembic migrations in `packages/database/` implement — this document
describes the *why* behind it, not a duplicate of the *what*.