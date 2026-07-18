"""RADAR platform-sim: a local-only simulator of an e-commerce platform.

One process simulates a *multi-service* e-commerce stack. It is not a set of
microservices and never will be: each scenario exposes its own domain metric and
its own chaos endpoint, and the alert rule watching that metric carries the
``service`` label of the service being simulated. So a single process can fire
alerts labelled ``service=order-service`` and ``service=checkout-service``
without either service existing. The service label lives in the alert rule
(``deploy/prometheus/alerting-rules.yml``), not in the metric.

This is a POC target, NOT a RADAR service. Prometheus scrapes its ``/metrics``
and evaluates the e-commerce alert rules against it; when a chaos endpoint
spikes a metric past a threshold, alertmanager fires a webhook at RADAR
ingestion. Driving that alert path end to end is the simulator's only role.

Deliberately absent (see the phase scope note): no Postgres, no transactional
outbox, no ``POST /events``, no ``/readyz``, no agent token. The standard RADAR
service template does NOT apply here. It is never deployed to Kubernetes.

Layout:

- ``metrics`` — the domain metrics the simulator exposes, grouped by the
  service each one belongs to.
- ``chaos`` — in-memory chaos state. A spike stores a value and an expiry
  timestamp; the metric is computed from that expiry at scrape time, so there
  is no background reset task to manage.
- ``main`` — FastAPI assembly: ``/metrics``, ``/healthz``, and the ``/chaos/*``
  endpoints.

Simulated services and their scenarios:

- ``order-service``    — ``POST /chaos/order-failures``
- ``checkout-service`` — ``POST /chaos/checkout-timeouts``

Endpoints:

- ``GET  /metrics``                 Prometheus text format.
- ``GET  /healthz``                 200 while the process is alive.
- ``POST /chaos/order-failures``    Spike ``order_processing_failure_rate``.
- ``POST /chaos/checkout-timeouts`` Spike ``checkout_timeout_rate``.
- ``POST /chaos/reset``             Clear active chaos for every scenario.
"""

from __future__ import annotations

__version__ = "0.5.0"

__all__ = ["__version__"]
