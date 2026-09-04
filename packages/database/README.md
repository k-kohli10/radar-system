# 🗄️ radar-database

The RADAR persistence layer, built on async SQLAlchemy 2.0 + asyncpg.

| Module | Provides |
|---|---|
| `connection` | Async engine and session factory |
| `models` | SQLAlchemy models for every table (alerts, incidents, investigation_plans, recommendations, feedback, outbox_events, processed_events, runbook_documents, audit_log) |
| `outbox` | Transactional outbox writer and `FOR UPDATE SKIP LOCKED` poller |
| `repository` | Per-model repositories, including the `processed_events` idempotency check |
| `migrations` | Alembic setup and the initial schema migration |

The transactional outbox is the backbone of RADAR's agent communication: events
are written in the same transaction as the state change that produced them, then
polled and dispatched by the outbox worker. See
[docs/adr/0003-postgres-outbox.md](../../docs/adr/0003-postgres-outbox.md).
