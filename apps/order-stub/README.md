# radar-order-stub

A local-only simulator of an e-commerce `order-service`. Prometheus scrapes its
`/metrics`, evaluates the e-commerce alert rules against it, and — when a chaos
endpoint spikes a rate past a threshold — alertmanager fires a webhook at RADAR
ingestion. Driving that alert path end to end is the stub's only job.

**This is a POC target, not a RADAR service.** It is never deployed to
Kubernetes and deliberately has no Postgres, no transactional outbox, no
`POST /events`, no `/readyz`, and no agent token. The standard RADAR service
template does not apply to it.

## Endpoints

```
GET  /metrics                  Prometheus text format
GET  /healthz                  process liveness (200)
POST /chaos/order-failures     spike order_processing_failure_rate
POST /chaos/checkout-timeouts  spike checkout_timeout_rate
POST /chaos/reset              clear all active chaos
```

## Metrics

```
order_processing_failure_rate     gauge      fraction of orders failing (0.0-1.0)
checkout_timeout_rate             gauge      fraction of checkouts timing out (0.0-1.0)
order_request_duration_seconds    histogram  order request latency
inventory_check_duration_seconds  histogram  inventory check latency
order_requests_total              counter    total order requests handled
```

The two gauges are the ones chaos drives. The stub does not simulate order
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
scrape time — active while `now < deadline`, baseline afterwards. `POST
/chaos/reset` clears both spikes immediately.

## Run locally

```
uv run uvicorn radar_order_stub.main:app --port 8080

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
  -f apps/order-stub/Dockerfile -t radar-order-stub .
```
