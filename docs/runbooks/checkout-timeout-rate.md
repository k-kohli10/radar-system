---
runbook_id: checkout-timeout-rate
title: Checkout Timeout Rate
alert_name: CheckoutTimeoutRate
services:
  - checkout-service
severity: high
status: fixture
---

# Checkout Timeout Rate

## Summary

More than 10% of checkout attempts are exceeding their timeout budget and
failing before the customer can complete a purchase. Unlike a failure after
payment, nothing has been charged and no order exists — the customer simply
cannot buy, and usually retries or leaves.

`checkout-service` is an orchestrator: a single checkout blocks on an inventory
availability check and a payment authorization, in sequence. It owns very little
work of its own, so a timeout here is far more often a symptom of a slow
dependency than a fault in checkout itself. Establish which dependency before
touching `checkout-service`.

## Symptoms

- `CheckoutTimeoutRate` firing, `service=checkout-service`, `severity=high`.
- `checkout_timeout_rate` above 0.10. This alert has the thinnest magnitude
  margin of the ratio alerts, so treat sustained values in the 0.05–0.10 band as
  meaningful even though they do not fire.
- Checkout p95 latency pressed up against the timeout ceiling rather than
  distributed below it — the signature of requests being cut off by the budget
  rather than completing slowly.
- A dependency alert firing alongside this one, most often
  `InventoryCheckLatency` or `PaymentGatewayErrorRate`. Check for this before
  anything else; it usually names the real incident.
- Cart abandonment climbing, and retry traffic rising as customers resubmit —
  which adds load to the very dependency that is already slow.

## Impact

Directly revenue-affecting, and the most visible failure mode in the platform:
customers who cannot check out do not silently wait, they leave. At 10% timeout
rate roughly one in ten purchase attempts fails outright.

Nothing is charged and no order is created, so unlike
`order-service-high-failure-rate` there is no reconciliation debt afterwards.
The cost is entirely lost sales during the window plus whatever share of those
customers do not return.

Retry behaviour makes this self-amplifying: failed checkouts generate retries,
retries add load to the saturated dependency, and the timeout rate climbs
further. Expect the curve to steepen rather than plateau if the underlying cause
persists.

## Likely Causes

1. **Inventory check latency.** `inventory-service` is slow, and because
   checkout blocks on that call, its latency becomes checkout's timeout.
   Distinguishing signal: `InventoryCheckLatency` firing, and checkout timeouts
   clustering at the inventory step. This is the most common cause by a wide
   margin.
2. **Payment authorization slowness.** The payment gateway is responding slowly
   or erroring, consuming the remaining budget. Distinguishing signal:
   `PaymentGatewayErrorRate` firing, or authorization latency elevated while
   inventory is healthy.
3. **Timeout budget misconfiguration.** The sum of downstream timeouts exceeds
   checkout's own budget, so checkout gives up while a dependency is still
   legitimately working. Distinguishing signal: timeouts at a suspiciously
   consistent elapsed time, and no dependency alert firing at all.
4. **Recent deploy.** A new synchronous call added to the checkout path, or a
   timeout value changed. Distinguishing signal: step change aligned with a
   rollout.
5. **Connection pool saturation in checkout.** Under a traffic spike, requests
   queue waiting for an outbound connection and time out before the call is even
   made. Distinguishing signal: timeouts rising with traffic volume while every
   dependency reports normal latency for the requests it does receive.

## Investigation

1. **Check for firing dependency alerts first.** If `InventoryCheckLatency` or
   `PaymentGatewayErrorRate` is active, stop here and work that runbook. This
   alert is downstream of both and will clear when they do.
2. **Determine which step is timing out.** In Kibana, query
   `service:checkout-service AND event:checkout_timeout` over the last 30
   minutes and aggregate by the step field. Inventory versus payment versus
   neither splits the causes immediately.
3. **Compare latency at the boundary.** For the implicated dependency, compare
   the latency checkout observes against the latency that service reports for
   itself. A large gap means the time is going into the network or into
   connection acquisition, not into the dependency's own work — that points at
   cause 5, not at the dependency.
4. **Read the timeout distribution.** Timeouts tightly clustered at one elapsed
   value indicate a budget ceiling being hit (cause 3). A broad spread indicates
   genuine variable slowness upstream.
5. **Check deploys on checkout AND its dependencies.** `kubectl rollout history`
   for `checkout-service`, `inventory-service`, and `payment-gateway`. A change
   in any of the three can surface here.
6. **Check outbound pool saturation.** If active outbound connections sit at the
   pool maximum while dependency-side latency is normal, the queueing is local
   and cause 5 is confirmed.

## Resolution

**Inventory latency (cause 1):** work `inventory-check-latency`. This alert
clears on its own once inventory recovers. Do not raise checkout's timeout to
paper over it — a longer budget converts fast failures into slow ones and holds
connections open longer, which makes saturation worse.

**Payment slowness (cause 2):** work `payment-gateway-errors`. Same reasoning
applies: do not extend the budget to accommodate a degraded dependency.

**Timeout budget misconfiguration (cause 3):** make the downstream budgets sum
to less than checkout's own, with margin. Each dependency call needs a timeout
strictly smaller than the remaining budget at the point it is made, otherwise
checkout abandons work that would have succeeded.

**Recent deploy (cause 4):** roll back — `kubectl rollout undo
deployment/checkout-service -n ecommerce`. If the deploy added a synchronous
call to the checkout path, make it asynchronous or move it off the critical path
before shipping again.

**Pool saturation (cause 5):** raise the outbound connection pool ceiling and
scale `checkout-service` replicas. Verify that dependency-side latency really is
normal first — scaling into a slow dependency adds load to it and makes the
incident worse rather than better.

**Once resolved**, confirm recovery on `checkout_timeout_rate` rather than on
latency alone, and expect a brief spike in successful checkouts as queued
customers retry.

## Escalation

Page the checkout-service on-call. At `severity=high` on a revenue-affecting
path this warrants immediate attention even outside business hours.

Escalate to an incident with customer communications if the timeout rate exceeds
25%, or if it persists beyond 15 minutes — at that point a meaningful share of
the day's purchase attempts have failed and the business needs to know.

If the cause is a dependency, page that service's on-call directly rather than
relaying through checkout. Handing off through an intermediary team costs time
that a revenue-affecting incident does not have.

## Related

- `inventory-check-latency` — the most common upstream cause. Checkout blocks on
  the inventory availability call, so latency there arrives here as timeouts.
- `payment-gateway-errors` — the second most common upstream cause, when
  authorization calls consume the remaining timeout budget.
- `order-service-high-failure-rate` — the mirror image, and easy to confuse.
  There, payment succeeded and the order was lost afterwards; here, nothing was
  charged and no order exists. If customers report being charged, that runbook
  applies, not this one.
