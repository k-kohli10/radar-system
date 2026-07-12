"""RADAR order-stub: a local-only e-commerce order-service simulator.

This is a POC target, NOT a RADAR service. Prometheus scrapes its ``/metrics``
and evaluates the e-commerce alert rules against it; when a chaos endpoint
spikes a rate past a threshold, Prometheus alertmanager fires a webhook at
RADAR ingestion. That is the stub's only role in the system.

Deliberately absent (see the phase scope note): no Postgres, no transactional
outbox, no ``POST /events``, no ``/readyz``, no agent token. The standard RADAR
service template does NOT apply here. It is never deployed to Kubernetes.

Layout:

- ``metrics`` — the five Prometheus metrics the stub exposes and the two
  gauges chaos drives.
- ``chaos`` — in-memory chaos state. A spike stores a rate and an expiry
  timestamp; the gauge value is computed from that expiry at scrape time, so
  there is no background reset task to manage.
- ``main`` — FastAPI assembly: ``/metrics``, ``/healthz``, and the ``/chaos/*``
  endpoints.

Endpoints:

- ``GET  /metrics``                 Prometheus text format.
- ``GET  /healthz``                 200 while the process is alive.
- ``POST /chaos/order-failures``    Spike ``order_processing_failure_rate``.
- ``POST /chaos/checkout-timeouts`` Spike ``checkout_timeout_rate``.
- ``POST /chaos/reset``             Clear all active chaos.
"""

from __future__ import annotations

__version__ = "0.5.0"

__all__ = ["__version__"]
