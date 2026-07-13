# radar-testing

Shared pytest fixtures for RADAR suites that need a **real Postgres**.

Several of RADAR's guarantees *are* Postgres guarantees — `FOR UPDATE SKIP
LOCKED` row locking, transaction-scoped `NOW()`, deferrable foreign keys, real
concurrent transactions. None of them exist against SQLite and a mock has no
locking at all, so the suites that assert them need a running Postgres and must
skip cleanly when there is none.

This package owns that setup once. It replaces three byte-identical copies of
the same DSN-resolution / schema-build / truncation block (`packages/database`,
`apps/ingestion`, `apps/outbox-worker`).

It is **test-only support code**, consumed as a dev dependency, so it never
enters a runtime package's import surface.

```python
# conftest.py
from radar_testing.postgres import database_url, db

__all__ = ["database_url", "db"]
```
