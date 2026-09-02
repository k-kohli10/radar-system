---
runbook_id: inventory-check-latency
title: Inventory Check Latency
alert_name: InventoryCheckLatency
services:
  - inventory-service
severity: high
status: fixture
---

# Inventory Check Latency

## Summary

Inventory availability checks are taking more than 500ms at p95. The service is
answering (this is a latency alert, not an error alert), but slowly enough that
callers blocking on it are affected.

`inventory-service` sits on the synchronous checkout path, so its latency is
someone else's timeout. This alert usually fires *before* `CheckoutTimeoutRate`
and is the earlier, more actionable signal of the same underlying problem.
Treating it as the root incident, rather than waiting for checkout to start
failing, is the difference between a latency blip and lost revenue.

## Symptoms

- `InventoryCheckLatency` firing, `service=inventory-service`, `severity=high`.
- `inventory_check_p95_seconds` above 0.5. Read p95 against p50 deliberately: p95
  elevated while p50 stays flat means a *subset* of requests is slow (a hot SKU,
  a lock, a cold cache path), whereas both rising together means the service is
  uniformly saturated. The two lead to different causes and different fixes.
- `CheckoutTimeoutRate` firing shortly afterwards, as checkout's budget is
  consumed by this call.
- Elevated database query time on the inventory tables, often concentrated in
  one query shape rather than spread across many.
- In the lock-contention case, active queries pile up while CPU stays low: the
  service is waiting, not working, which is the clearest signal that adding
  replicas will not help.

## Impact

Customer-facing but indirect. Nobody sees an inventory error; they see checkout
being slow, and then, if this persists, checkout failing outright.

The severity is driven by what sits downstream rather than by this service's own
health. Checkout blocks on this call, so sustained latency here becomes lost
purchases within minutes. `order-service` also reserves stock through this
service, so prolonged slowness surfaces there as failed order processing.

Below roughly 1s p95 the effect is degraded experience. Above that, checkout
timeouts begin in volume and this becomes revenue-affecting.

## Likely Causes

1. **Lock contention on hot SKUs.** Many concurrent checkouts reserving the same
   product serialise on the same stock rows. Distinguishing signal: p95 elevated
   with p50 flat, database lock waits climbing, CPU low. Classic during flash
   sales, promotions, and launches.
2. **Missing or unused index.** A query planner regression or a new query shape
   causing sequential scans. Distinguishing signal: one query shape dominating
   database time, and latency that steps up rather than correlating with traffic.
3. **Cache miss storm.** A cache flush, restart, or mass expiry sends
   availability lookups to the database simultaneously. Distinguishing signal: a
   sharp spike that decays as the cache refills, correlated with a deploy or
   cache restart.
4. **Read replica lag or failover.** Reads served by a lagging replica, or all
   traffic landing on the primary after a failover. Distinguishing signal:
   replica lag metrics elevated, or a step change with no corresponding
   application deploy.
5. **Traffic genuinely above provisioned capacity.** A sale or a marketing send
   drives real load beyond what the service is sized for. Distinguishing signal:
   p50 and p95 rising together, CPU high, latency tracking request volume.

## Investigation

1. **Compare p50 against p95 first.** This single comparison splits the causes:
   p95-only elevation points at contention or a slow subset (causes 1–3), both
   rising together points at saturation (cause 5). Do this before opening any
   database console.
2. **Check for lock waits.** In the database, look at active queries waiting on
   locks against the inventory tables. Sustained waiters with low CPU is cause 1
   and means scaling replicas will not help: the bottleneck is a row, not a CPU.
3. **Find the dominant query shape.** Aggregate database time by normalised
   query over the last 30 minutes. One shape dominating points at cause 2; a
   sudden shift in which shape dominates suggests a plan change.
4. **Check cache hit rate.** A hit rate that has dropped sharply is cause 3, and
   the recovery curve tells you whether it is refilling on its own or being
   continuously invalidated.
5. **Check replica health and lag.** Confirm reads are landing where they should
   and that no failover happened in the window. A failover is easy to miss
   because nothing in the application changed.
6. **Correlate with traffic and promotions.** Compare request volume against the
   same window yesterday. A promotion or scheduled send explains cause 5 and
   changes the fix from "repair something broken" to "provision for real load."

## Resolution

**Lock contention (cause 1):** shorten the transaction holding the stock row
(move non-essential work out of it) and reduce the window between read and
update. Where the product allows, switch hot SKUs to an optimistic reservation
scheme rather than a pessimistic row lock. Adding replicas does not help
contention and will make it slightly worse by adding connections.

**Missing index (cause 2):** add the index for the dominant query shape, or
force a plan if the planner has regressed on unchanged data. Verify with an
execution plan rather than by watching the latency graph, so the fix is
attributable.

**Cache miss storm (cause 3):** let it refill if it is decaying on its own. If
it is not, find what is invalidating continuously: a deploy loop, a cache
key change, an unbounded TTL jitter. Stagger expiry so mass simultaneous
invalidation cannot recur.

**Replica issue (cause 4):** route reads away from the lagging replica until it
catches up. If a failover occurred, confirm the new topology is what you expect
before assuming the application is at fault.

**Capacity (cause 5):** scale replicas and, if the load is expected to persist,
raise the provisioned capacity permanently. Real traffic exceeding provisioning
is a planning outcome, not a bug: record it so the next promotion is sized for.

**In all cases**, confirm recovery on `inventory_check_p95_seconds` and then
verify `CheckoutTimeoutRate` clears. This alert clearing while checkout still
times out means there is a second cause downstream.

## Escalation

Page the inventory-service on-call at `severity=high`, and do so even if
checkout has not started failing yet: this alert exists to be acted on before
that happens.

Escalate to an incident if p95 exceeds 1s, if `CheckoutTimeoutRate` begins
firing, or if the cause is lock contention during an active promotion: that
combination will not resolve on its own and typically worsens as the promotion
drives more concurrent traffic to the same SKUs.

Bring in the database on-call for lock contention or suspected plan regressions.
Both are diagnosed faster with someone who can read the database's own
diagnostics directly.

## Related

- `checkout-timeout-rate`: the downstream symptom. Checkout blocks on this
  call, so this runbook is usually the root incident when both are firing. Work
  this one first.
- `order-service-high-failure-rate`: order processing reserves stock through
  this service, so prolonged latency here surfaces there as failed orders.
- `order-service-high-memory`: unrelated cause, occasionally confused because
  both present as "a service is slow." Memory pressure produces GC pauses across
  all requests uniformly; this runbook covers latency concentrated in inventory
  lookups specifically.
