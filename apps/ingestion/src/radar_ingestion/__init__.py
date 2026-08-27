"""RADAR ingestion service: the platform entry point.

Detection happens outside RADAR (Prometheus alertmanager, Kibana Watcher).
Ingestion is where those pre-fired alerts enter: it receives them on per-source
webhook endpoints, normalizes each vendor payload to the vendor-neutral
:class:`radar_contracts.NormalizedAlert`, deduplicates by fingerprint within a time
window, and transactionally attaches the alert to an open incident or opens a new
one — writing an ``alert.normalized`` outbox event for the watcher either way.
Duplicates are published too, tagged ``deduplicated``, because the watcher owns
suppression and escalation and escalation is a claim about arrival rate it cannot
enforce against alerts it is never shown.

Ingestion is NOT an agent (ADR 0011):

- Inbound ``/alerts/*`` authenticate with a per-source ``X-Radar-Webhook-Token``
  loaded from Vault — never the internal ``X-Radar-Agent-Token``.
- There is no ``POST /events``; ingestion produces outbox events, it does not
  consume them.

Layout:

- ``config`` — non-secret settings plus the Postgres DSN, read from a Vault
  secret file (the DSN embeds a password, so it is never an env var).
- ``routes`` — the source-routed ``/alerts/{prometheus,kibana,mock}`` endpoints.
- ``normalizer`` — per-source vendor payload -> ``NormalizedAlert``.
- ``deduper`` — fingerprint + time-window duplicate detection.
- ``publisher`` — transactional incident creation and outbox publish.
- ``security`` — the ``X-Radar-Webhook-Token`` inbound auth dependency.
- ``main`` — FastAPI assembly: ``/alerts/*``, ``/healthz``, ``/readyz``,
  ``/metrics``, request metrics, and OTel tracing.
"""

from __future__ import annotations

__version__ = "0.5.0"

__all__ = ["__version__"]
