"""RADAR outbox worker: the transport that moves work between agents.

The outbox worker is the first *consumer* in the pipeline. RADAR services never
call each other over HTTP directly; a producer writes an ``outbox_events`` row in
the same transaction as its state change, and this worker delivers it. It polls
``outbox_events``, claims due ``pending`` rows with ``FOR UPDATE SKIP LOCKED`` so
no two workers ever claim the same event, and dispatches each via
``POST /events`` to its ``target_service`` — authenticating outbound as the
worker with ``X-Radar-Agent-Token``.

It is a consumer, not an agent endpoint: it has **no** ``POST /events`` of its
own. It exposes only the standard operational surface (``/healthz``, ``/readyz``,
``/metrics``) plus token-guarded admin endpoints for the dead-letter queue.

Delivery semantics (see the outbox worker specification in the implementation
plan and docs/adr/0003-postgres-outbox.md):

- **Claim → commit → dispatch → mark.** The claim transaction commits before any
  HTTP call, so row locks are never held across dispatch.
- **Bounded retry with backoff.** A failed dispatch is rescheduled with growing
  ``process_after`` delays; after the final attempt the event is promoted to
  ``dead_letter`` with an ``audit_log`` record.
- **Crash recovery (reaper).** Events stranded in ``processing`` by a crashed
  worker are re-pended through the same failure path — incrementing ``attempts``
  so every event reaches a terminal state (delivered or dead-lettered).

Layout:

- ``poller`` — the claim-and-drive loop over due ``pending`` events.
- ``dispatcher`` — the outbound ``POST /events`` HTTP client with a hard timeout.
- ``retry`` — backoff rescheduling of failed dispatches.
- ``dead_letter`` — terminal promotion, audit record, and admin list/requeue.
- ``main`` — the lightweight app: ``/healthz``, ``/readyz``, ``/metrics``, admin
  endpoints, metrics, tracing, and the poller's lifecycle.
"""

from __future__ import annotations

__version__ = "0.6.0"

__all__ = ["__version__"]
