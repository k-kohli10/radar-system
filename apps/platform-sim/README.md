# radar-platform-sim

A local-only, **single-process simulator of a multi-service e-commerce
platform**. Prometheus scrapes its `/metrics` and evaluates the e-commerce alert
rules against it. When a chaos endpoint spikes a metric past a threshold,
alertmanager fires a webhook at RADAR ingestion. Driving that alert path end to
end is the simulator's only job.

It is **not** a set of microservices and never will be. One process exposes a
domain metric and a chaos endpoint per scenario; the alert rule watching each
metric carries the `service` label of the service being simulated. That is how a
single process fires alerts labelled `service=order-service` and
`service=checkout-service` without either service existing. The service label
lives in the alert rule, not in the metric.

**This is a POC target, not a RADAR service.** It is never deployed to
Kubernetes and deliberately has no Postgres, no transactional outbox, no
`POST /events`, no `/readyz`, and no agent token. The standard RADAR service
template does not apply to it.

## Simulated services

| service | scenario | chaos endpoint |
|---|---|---|
| `order-service` | order processing failures | `POST /chaos/order-failures` |
| `checkout-service` | checkout timeouts | `POST /chaos/checkout-timeouts` |

## Endpoints

```
GET  /metrics                  Prometheus text format
GET  /healthz                  process liveness (200)
POST /chaos/order-failures     spike order_processing_failure_rate
POST /chaos/checkout-timeouts  spike checkout_timeout_rate
POST /chaos/reset              clear active chaos for every scenario
```

## Metrics

```
order_processing_failure_rate     gauge      fraction of orders failing (0.0-1.0)
checkout_timeout_rate             gauge      fraction of checkouts timing out (0.0-1.0)
order_request_duration_seconds    histogram  order request latency
inventory_check_duration_seconds  histogram  inventory check latency
order_requests_total              counter    total order requests handled
```

The two gauges are the ones chaos drives. The simulator does not simulate
traffic, so the counter and histograms are exposed for scraping completeness and
read zero at rest.

## Chaos

`POST /chaos/order-failures` and `POST /chaos/checkout-timeouts` take:

```json
{"rate": 0.15, "duration_seconds": 120}
```

`rate` (0.0–1.0) is pinned onto the target gauge for `duration_seconds`, then
the gauge returns to its `0.0` baseline. There is no background reset task: a
spike stores a monotonic **deadline** and the gauge value is computed from it at
scrape time: active while `now < deadline`, baseline afterwards. `POST
/chaos/reset` clears every scenario's spike immediately.

## Run locally

```
uv run uvicorn radar_platform_sim.main:app --port 8080

# fire order-service into a failing state for 2 minutes
curl -sX POST localhost:8080/chaos/order-failures \
  -H 'content-type: application/json' \
  -d '{"rate": 0.15, "duration_seconds": 120}'

# watch the gauge
curl -s localhost:8080/metrics | grep order_processing_failure_rate
```

## Docker

Build from the **repo root** (uv workspace resolves against the root lockfile):

```
docker buildx build --platform linux/amd64,linux/arm64 \
  -f apps/platform-sim/Dockerfile -t radar-platform-sim .
```
