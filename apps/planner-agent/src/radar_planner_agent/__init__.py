"""RADAR planner agent: the second stage of the incident pipeline.

The planner turns an incident into an *investigation*. It consumes
``incident.plan_requested`` (dispatched by the outbox worker, never called
directly by the watcher), matches a YAML template on ``service_name:alert_name``,
stores the resulting investigation plan, and asks the reasoner to reason over it.

**This is the simple service, deliberately.** No LLM, no correlation, no time
windows — a YAML lookup and one transactional write. The investigation steps are
config, so an SRE improves them with a ConfigMap edit rather than a deploy, and
the reasoner gets a concrete checklist to ground its analysis in rather than
being asked to invent one.

``_default`` is required, not optional: an alert with no matching template still
gets a generic investigation, so no incident is ever left unplanned merely
because nobody wrote a template for it.

Layout:

- ``config`` — non-secret settings plus the Postgres DSN from a Vault secret file.
- ``routes`` — ``POST /events``: the ``processed_events`` gate, then the work.
- ``main`` — FastAPI assembly: ``/events``, ``/healthz``, ``/readyz``,
  ``/metrics``, request metrics, and OTel tracing. The inbound agent-token guard
  (and 401-before-422) is the shared one from ``radar_common``.
"""

from __future__ import annotations

__version__ = "0.7.0"

__all__ = ["__version__"]
