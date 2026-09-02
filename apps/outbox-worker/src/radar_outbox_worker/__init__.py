"""RADAR outbox worker: the transport that moves work between agents.

A producer writes an ``outbox_events`` row in the same transaction as its state
change; this worker polls the table, claims due ``pending`` rows with
``FOR UPDATE SKIP LOCKED``, and dispatches each via ``POST /events`` to its
``target_service``, authenticating outbound with that target's
``X-Radar-Agent-Token``. It has no ``POST /events`` of its own: only
``/healthz``, ``/readyz``, ``/metrics``, and token-guarded dead-letter admin
endpoints.

Delivery semantics (see docs/adr/0003-postgres-outbox.md):

- **Claim → commit → dispatch → mark.** The claim transaction commits before any
  HTTP call, so row locks are never held across dispatch.
- **Disjoint claims.** ``SKIP LOCKED`` means two workers never claim the same event.
- **Bounded retry with backoff.** A failed dispatch is rescheduled with growing
  ``process_after`` delays; after the final attempt it is promoted to
  ``dead_letter`` with an ``audit_log`` record.
- **Crash recovery (reaper).** Events stranded in ``processing`` are re-pended
  through the same failure path, incrementing ``attempts`` so every event reaches
  a terminal state.

Layout:

- ``poller`` - the claim-and-drive loop over due ``pending`` events.
- ``dispatcher`` - the outbound ``POST /events`` client with a hard timeout.
- ``retry`` - backoff rescheduling of failed dispatches.
- ``dead_letter`` - terminal promotion, audit record, and admin list/requeue.
- ``main`` - FastAPI assembly and the background tasks' lifecycle.
"""

from __future__ import annotations

__version__ = "0.6.0"

__all__ = ["__version__"]
