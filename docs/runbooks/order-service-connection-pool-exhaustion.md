---
runbook_id: order-service-connection-pool-exhaustion
title: Order Service Connection Pool Exhaustion
services:
  - order-service
severity: high
status: fixture
---

# Order Service Connection Pool Exhaustion

## Summary

`order-service` has run out of available database connections. Requests are not
failing on any database error — they are queueing to acquire a connection and
timing out before they get one. The database itself is typically healthy and
underloaded throughout.

This is the failure mode most often misdiagnosed as "the database is slow,"
because every symptom points at the database while the bottleneck is entirely on
the application side. The distinguishing question is where the time is spent:
waiting *for* a connection, or waiting *on* a query. Those have opposite fixes,
and doing the database-side one makes the application-side problem worse.

## Symptoms

- Order processing latency rising sharply with no corresponding rise in database
  query time. This gap is the signature.
- Connection acquisition wait time climbing, and active connections pinned at
  the configured pool maximum rather than fluctuating below it.
- Timeout errors mentioning connection acquisition or pool checkout rather than
  query execution or statement timeouts.
- Database-side CPU, IO, and query latency all normal — often conspicuously
  idle, because the application is not sending it enough work.
- The problem worsens with load in a step rather than a curve: below the pool
  ceiling everything is fine, above it everything queues at once.

## Impact

Order processing slows and then fails as requests exhaust their own timeouts
waiting for a connection. If sustained, this surfaces as
`OrderProcessingFailureRate` and is one of the causes that runbook lists.

The failure is total rather than partial once the pool is saturated: every
request needs a connection, so no request can complete, and the service appears
hung rather than degraded. Restarting clears it temporarily, which is what makes
a leak easy to misread as a transient glitch.

Under a leak the time-to-failure is predictable — the pool drains at a roughly
constant rate — so a service that fails every few hours after a restart is
leaking, not overloaded.

## Likely Causes

1. **Connection leak on an error path.** Code acquires a connection and returns
   it on the success path only, so every error permanently loses one.
   Distinguishing signal: active connections rise monotonically and never fall,
   including during traffic troughs, and the rise correlates with error volume
   rather than request volume. This is the most common cause.
2. **Long-running transactions holding connections.** A transaction kept open
   across slow work — an external call, a large computation — holds its
   connection for the duration. Distinguishing signal: few active queries but
   many open transactions, and connection hold time far exceeding query time.
3. **Pool undersized for real concurrency.** The pool was sized for lower
   traffic than the service now receives. Distinguishing signal: saturation only
   at peak, full recovery off-peak, and no monotonic growth.
4. **Downstream slowness backing up into the pool.** A slow dependency holds
   request threads open, each holding a connection acquired earlier.
   Distinguishing signal: `InventoryCheckLatency` or a payment alert firing at
   the same time, with pool saturation arriving second.
5. **Pool larger than the database allows.** The sum of pool sizes across
   replicas exceeds the database's own connection limit, so connections fail to
   establish rather than queueing. Distinguishing signal: errors about too many
   clients, appearing as replicas scale up rather than as traffic rises.

## Investigation

1. **Split acquisition time from query time.** This is the decisive measurement
   and it comes before everything else. High acquisition with normal query time
   confirms pool exhaustion; both high points at the database and means you are
   in the wrong runbook.
2. **Plot active connections over 24 hours.** Monotonic growth that survives
   overnight troughs is cause 1. Peak-correlated saturation with off-peak
   recovery is cause 3. A step change is cause 2 or a deploy.
3. **Correlate connection growth with error rate.** If active connections track
   cumulative *errors* rather than cumulative requests, the leak is on an error
   path — which narrows the code search to exception handling around database
   access.
4. **Check for long-open transactions.** In the database, list transactions open
   longer than a few seconds. A transaction open for minutes is cause 2, and the
   query it is idle inside names the code path.
5. **Compare pool total against the database limit.** Multiply the per-replica
   pool maximum by the replica count and compare against the database's
   configured maximum connections. Exceeding it is cause 5 and gets worse every
   time the service scales out.
6. **Check dependency alerts.** If a downstream alert fired first, treat this as
   a secondary effect and work that runbook — enlarging the pool while a
   dependency is slow just moves the queue.

## Resolution

**Leak (cause 1):** the fix is in code — acquire connections with a construct
that releases on every path, including exceptions. As immediate relief, restart
the affected replicas; this resets the pool and buys time proportional to the
leak rate. Track that time, because it tells you how long you have before the
next occurrence.

**Long transactions (cause 2):** move non-database work out of the transaction.
An external call inside a transaction is the usual culprit, and it holds a
connection for the entire round trip. Shorten the transaction rather than
enlarging the pool.

**Undersized pool (cause 3):** raise the pool maximum, but check cause 5 first —
raising it past the database's own limit converts queueing into connection
errors, which is worse. Scale the database's connection capacity or introduce a
connection proxy if the ceiling is genuinely reached.

**Downstream slowness (cause 4):** work the dependency's runbook. This clears on
its own once the dependency recovers.

**Over-provisioned pool (cause 5):** reduce per-replica pool size so the product
of pool size and replica count sits safely under the database limit, with
headroom for maintenance connections. Recompute this whenever the replica count
or HPA ceiling changes — it is a product, and it grows silently when either
factor does.

## Escalation

Page the order-service on-call if order processing is failing. If latency is
elevated but orders still complete, this is urgent but not a page — the pool has
not fully saturated yet.

Escalate to the database on-call for cause 5, or if the database's own
connection limit needs raising. That change has implications beyond this service
and should not be made unilaterally.

If restarting resolves it and it returns on a predictable schedule, treat that as
a confirmed leak and escalate to the service owner as a bug rather than
re-restarting indefinitely. A scheduled restart papering over a leak is a
decision someone should make deliberately, not a habit that forms by default.

## Related

- `order-service-high-failure-rate` — the alert this usually surfaces as, where
  pool exhaustion is listed as cause 5. That runbook covers orders failing for
  any reason; this one covers the specific case where they fail waiting for a
  connection.
- `order-service-high-memory` — the other resource-exhaustion failure in this
  service. Memory pressure produces GC pauses and OOM restarts; pool exhaustion
  produces acquisition timeouts with a healthy heap. Check which resource is
  actually exhausted before assuming.
- `inventory-check-latency` — a common upstream trigger, since dependency
  slowness holds request threads and their connections open.
