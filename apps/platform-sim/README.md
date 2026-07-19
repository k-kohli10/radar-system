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
| `order-service` | memory pressure | `POST /chaos/order-memory` |
| `checkout-service` | checkout timeouts | `POST /chaos/checkout-timeouts` |
| `payment-gateway` | gateway authorization errors | `POST /chaos/payment-errors` |
| `payment-gateway` | issuer card declines | `POST /chaos/payment-declines` |
| `inventory-service` | slow availability checks | `POST /chaos/inventory-latency` |

## Endpoints

```
GET  /metrics                  Prometheus text format
GET  /healthz                  process liveness (200)
POST /chaos/order-failures     spike order_processing_failure_rate
POST /chaos/checkout-timeouts  spike checkout_timeout_rate
POST /chaos/payment-errors     spike payment_gateway_error_rate
POST /chaos/payment-declines   ramp  payment_declines_total
POST /chaos/inventory-latency  spike inventory_check_p95_seconds
POST /chaos/order-memory       spike order_service_memory_bytes
POST /chaos/reset              clear active chaos for every scenario
```

## Metrics

```
order_processing_failure_rate     gauge      fraction of orders failing (0.0-1.0)
checkout_timeout_rate             gauge      fraction of checkouts timing out (0.0-1.0)
payment_gateway_error_rate        gauge      fraction of authorizations erroring (0.0-1.0)
payment_declines_total            counter    card payments declined by the issuer
inventory_check_p95_seconds       gauge      inventory check p95 latency in seconds
order_service_memory_bytes        gauge      order-service resident memory in bytes
order_request_duration_seconds    histogram  order request latency
order_requests_total              counter    total order requests handled
```

Everything except `order_request_duration_seconds` and `order_requests_total` is
chaos-driven. The simulator does not simulate traffic, so those two are exposed
for scraping completeness and read zero at rest.

Inventory latency is a **gauge holding a p95**, not a histogram. A histogram
cannot be pinned — `histogram_quantile` over `rate(..._bucket[5m])` needs real
observations accruing over time, which the deadline design cannot produce, and
faking them would couple the metric to scrape cadence.

## Chaos

Two request shapes, because gauges and counters behave differently.

**Gauges** — `/chaos/order-failures`, `/chaos/checkout-timeouts`,
`/chaos/payment-errors`:

```json
{"rate": 0.15, "duration_seconds": 120}
```

`rate` (0.0–1.0) is pinned onto the target gauge for `duration_seconds`, then
the gauge returns to its `0.0` baseline. There is no background reset task: a
spike stores a monotonic **deadline** and the gauge value is computed from it at
scrape time: active while `now < deadline`, baseline afterwards.

**Absolute gauges** — `/chaos/inventory-latency`, `/chaos/order-memory`:

```json
{"value": 1.5, "duration_seconds": 120}
```

Same pin-until-deadline behaviour, but `value` is an absolute quantity in the
metric's own unit (seconds for latency, bytes for memory) rather than a
fraction, so it is deliberately
not capped at 1.0. The ratio endpoints keep their `0.0-1.0` bound: it rejects
`{"rate": 15}` from someone who meant 15%, which would otherwise breach every
ratio rule at once while looking like a successful spike.

**Counters** — `/chaos/payment-declines`:

```json
{"per_second": 10.0, "duration_seconds": 300}
```

A counter cannot be pinned: what the alert rule reads is `rate()`, the slope, so
the metric has to *evolve* rather than hold. Each scrape advances the counter by
`per_second × elapsed`, counting only time inside the active window, so two
scrapes apart genuinely differ. `per_second` is events per second and is not
capped at 1.0. Whole events only — a fractional remainder is carried to the next
scrape rather than rounded away.

Note that the counter only moves when something scrapes `/metrics`. That is not
a limitation: with no scrapes there is no `rate()` to observe in the first place.

`POST /chaos/reset` clears every scenario immediately. It stops the decline ramp
but **does not rewind** the counter — a counter going backwards tells Prometheus
the process restarted, and `rate()` discards that interval, which would corrupt
the very query the alert rule runs.

### Spike values that actually fire

Each rule in `deploy/prometheus/alerting-rules.yml` has both a magnitude and a
duration bar; a spike must clear both, and a spike shorter than the rule's `for`
never fires however large it is. The measured minimums are tabulated in that
file's header.

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
