"""RADAR watcher agent: the first stage of the incident pipeline.

The watcher consumes ``alert.normalized`` events (dispatched to it by the outbox
worker, never called directly by ingestion) and decides what the pipeline should do
about the incident that alert landed on: escalate its severity, suppress a
follow-on, or request an investigation plan.

**Ingestion owns incident identity; the watcher owns correlation policy.** By the
time an event arrives here, ingestion has already opened or matched the incident and
written the alert row — the watcher never inserts either. What it adds is judgement
over time: whether alerts are arriving fast enough to escalate, whether this incident
follows too closely on a recent one to be worth investigating again, and — when it is
worth it — an ``incident.plan_requested`` event for the planner.

Every alert reaches it, duplicates included (see the ingestion publisher). That is
what makes escalation observable at all: a rule about arrival rate cannot be enforced
against alerts you are never shown.

**No LLM.** Correlation policy is YAML — deliberately, so it is auditable, testable,
and changeable without a deploy. The reasoner is the only stage that calls a model.

Layout:

- ``config`` — non-secret settings plus the Postgres DSN from a Vault secret file.
- ``routes`` — ``POST /events``: the ``processed_events`` gate, then the work.
- ``security`` — the inbound ``X-Radar-Agent-Token`` guard, and 401-before-422.
- ``main`` — FastAPI assembly: ``/events``, ``/healthz``, ``/readyz``, ``/metrics``,
  request metrics, and OTel tracing.
"""

from __future__ import annotations

__version__ = "0.7.0"

__all__ = ["__version__"]
