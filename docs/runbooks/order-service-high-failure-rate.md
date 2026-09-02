---
runbook_id: order-service-high-failure-rate
title: Order Service High Failure Rate
alert_name: OrderProcessingFailureRate
services:
  - order-service
severity: critical
status: fixture
---

# Order Service High Failure Rate

## Summary

`order-service` is accepting orders but failing to process them to completion.
Checkout succeeds from the customer's point of view: payment is authorized and
the confirmation page renders: but the order never reaches the fulfilment
queue. The gap between "customer believes they bought it" and "the warehouse
knows about it" is what makes this critical rather than merely degraded.

The alert fires when `order_processing_failure_rate` exceeds 5% for one minute.
Baseline is well under 0.5%; anything sustained above 2% is already abnormal
even if it has not yet tripped the threshold.

## Symptoms

- `OrderProcessingFailureRate` firing, `service=order-service`,
  `severity=critical`.
- Elevated `order_processing_failure_rate` on the Order Pipeline dashboard,
  usually a step change rather than a ramp: step changes point at a deploy or a
  dependency flipping state, ramps point at resource exhaustion.
- `order-service` error logs showing repeated failures at the same pipeline
  stage. The stage is the diagnostic signal; a spread across many stages means
  something shared underneath is broken, not the stage itself.
- Support tickets reporting "I was charged but my order is not in my account."
  These lag the alert by roughly 15–30 minutes.
- Fulfilment queue depth flat or falling while checkout volume is normal: the
  clearest confirmation that orders are being lost after payment.

## Impact

Revenue-affecting and trust-affecting simultaneously. Customers have been
charged for orders that do not exist downstream, so every minute of this
produces reconciliation work and refund requests regardless of how quickly the
underlying fault is fixed.

At 5% failure with typical daytime volume this is roughly 40–60 orders per hour
requiring manual recovery. Above 20%, treat it as an incident warranting a
checkout pause: continuing to take payments the pipeline cannot honour makes the
cleanup worse rather than preserving revenue.

## Likely Causes

1. **Recent deploy.** By far the most common. A schema change, a validation rule
   tightened without a migration, or a serialization change in the fulfilment
   message. Distinguishing signal: failure rate steps up within minutes of a
   rollout, and the failures concentrate at one pipeline stage.
2. **Fulfilment queue publish failures.** The broker is reachable for consumers
   but rejecting publishes: full queue, expired credentials, or a topic ACL
   change. Distinguishing signal: order rows are written to Postgres correctly,
   but no corresponding publish is logged.
3. **Database constraint violations.** A unique or foreign-key constraint
   rejecting inserts, typically after a partially-applied migration or a
   backfill that left orphaned references. Distinguishing signal: failures carry
   a Postgres SQLSTATE, most often `23505` or `23503`.
4. **Inventory reservation failures.** `inventory-service` returning errors or
   timing out, so orders cannot reserve stock and fail closed. Distinguishing
   signal: `InventoryCheckLatency` firing alongside this alert: if so, treat
   inventory as the primary incident and this as its symptom.
5. **Connection pool exhaustion.** Under sustained load the pool saturates and
   order writes time out waiting for a connection. Distinguishing signal: a
   ramp rather than a step, plus latency rising ahead of the failure rate.

## Investigation

1. **Check for a recent deploy first.** `kubectl rollout history
   deployment/order-service -n ecommerce`. If anything shipped within 30 minutes
   of the alert, treat it as the prime suspect and skip to Resolution: do not
   spend twenty minutes on root cause before rolling back.
2. **Identify the failing stage.** In Kibana, query
   `service:order-service AND level:error` over the last 30 minutes and
   aggregate by the pipeline stage field. One dominant stage narrows the cause
   immediately; an even spread points at a shared dependency.
3. **Read the actual error, not the count.** Pull ten representative error
   documents in full. A Postgres SQLSTATE indicates cause 3; a broker publish
   timeout indicates cause 2; an upstream 5xx from `inventory-service` indicates
   cause 4.
4. **Compare writes against publishes.** Count order rows created in the window
   against fulfilment publishes logged in the same window. Rows without
   publishes is cause 2 and tells you the recovery set: those specific orders
   need replaying.
5. **Check dependency health.** Confirm whether `InventoryCheckLatency` or any
   `payment-gateway` alert is firing. If a dependency alert is also active, this
   runbook is probably not the incident you want to be working.
6. **Check pool saturation.** Active connections against pool maximum on the
   Order Pipeline dashboard. Sustained saturation is cause 5 and explains a ramp
   that none of the other causes would produce.

## Resolution

**Recent deploy (cause 1):** roll back: `kubectl rollout undo
deployment/order-service -n ecommerce`. Confirm `order_processing_failure_rate`
returns to baseline within two minutes of pods becoming ready. Root-cause the
bad build after service is restored, not before.

**Queue publish failures (cause 2):** restore publishing (renew credentials,
drain the queue, or revert the ACL change), then replay the affected orders from
the order table. Replay is idempotent, it keys on order id, so replaying an
order that did in fact publish is safe.

**Constraint violations (cause 3):** identify the constraint from the SQLSTATE
and the table. If a migration is partially applied, complete or reverse it
deliberately; do not drop the constraint to clear the error, because that
converts a loud failure into silent data corruption.

**Inventory failures (cause 4):** work `inventory-check-latency` instead. This
alert should clear on its own once inventory recovers.

**Pool exhaustion (cause 5):** raise the pool ceiling as immediate relief and
scale replicas if load is genuinely higher than provisioned. Persistent
exhaustion at normal volume means a connection leak: look for a code path that
acquires a connection without releasing it on the error branch.

**In all cases**, reconcile charged-but-unfulfilled orders before closing the
incident. The alert clearing means new orders are fine; it says nothing about
the ones already lost.

## Escalation

Page the order-service on-call immediately: this alert is critical and
auto-pages, so confirm someone has acknowledged rather than assuming.

Escalate to the engineering manager and open a customer-communications thread if
any of: failure rate above 20% for more than ten minutes, more than 500 orders
requiring manual reconciliation, or root cause still unidentified after 30
minutes.

If the cause is a partially-applied migration, bring in the database on-call
before taking any corrective action. Migration recovery under incident pressure
is where a bad incident becomes an unrecoverable one.

## Related

- `order-service-high-memory`: same service, different failure mode. Memory
  pressure produces restarts and dropped in-flight work; this runbook is about
  orders failing while the process stays healthy. If pods are restarting, that
  runbook is the right one.
- `inventory-check-latency`: a frequent upstream cause of this alert.
- `checkout-timeout-rate`: the customer-facing symptom when failures occur
  *before* payment rather than after.
