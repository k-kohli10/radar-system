---
runbook_id: order-service-high-memory
title: Order Service High Memory
alert_name: OrderServiceHighMemory
services:
  - order-service
severity: medium
status: fixture
---

# Order Service High Memory

## Summary

`order-service` resident memory has stayed above 1.5GB for five minutes.
Nothing is failing yet: this alert is a leading indicator, not an outage. It
fires early precisely so someone can look while the service is still healthy.

The trajectory is what matters. Memory that rises to a new plateau and holds is
usually a workload or configuration change and can wait for business hours.
Memory that climbs steadily without levelling off will reach the 2GB container
limit, get OOM-killed, and restart: dropping every in-flight order at the
moment it does.

## Symptoms

- `OrderServiceHighMemory` firing, `service=order-service`, `severity=medium`.
- `order_service_memory_bytes` above 1.5e9 on the Order Pipeline dashboard. Look
  at the shape over 24 hours, not the current value: sawtooth means the process
  is already being OOM-killed and restarting, a steady climb means a leak, a
  step to a flat plateau means a workload change.
- Garbage collection time trending up as the heap fills, visible as a gradual
  rise in request latency before anything errors.
- In the leak case, `kubectl get pods -n ecommerce` shows a non-zero
  `RESTARTS` count on `order-service` pods, climbing every few hours.
- Order processing itself is usually *normal* at this stage. If orders are also
  failing, the failure is the incident and this alert is a side effect.

## Impact

None yet, in the common case. This is the alert firing early enough that there
is still a choice about when to act.

The impact arrives at OOM-kill: the container is terminated without warning, so
in-flight orders are lost rather than drained, and each restart costs roughly
30–45 seconds of reduced capacity while the replacement pod warms up. If several
replicas are leaking on the same schedule they will OOM at roughly the same
time, which turns a rolling annoyance into a genuine capacity gap.

At `severity=medium` this does not auto-page. It is a working-hours
investigation unless the growth curve says the container limit arrives sooner.

## Likely Causes

1. **Unbounded in-process cache.** A cache with no eviction policy or TTL that
   grows with cardinality: product ids, customer ids, promo codes. Distinguishing
   signal: memory tracks a business quantity (catalogue size, active customers)
   rather than request volume, and never drops.
2. **Genuine memory leak.** Objects retained by a listener, a background task,
   or an accumulating list that is never cleared. Distinguishing signal: steady
   climb that survives traffic troughs: memory does not fall overnight when
   almost no requests arrive.
3. **Recent deploy raising the baseline.** A new dependency, a larger connection
   pool, or increased worker concurrency, each of which legitimately costs
   memory. Distinguishing signal: a single step up at deploy time, then flat.
   This is not a leak and often needs a limit increase, not a code fix.
4. **Large batch or bulk operation.** A backfill, a bulk import, or an oversized
   report holding a whole result set in memory. Distinguishing signal: a spike
   correlated with a scheduled job, returning to baseline afterwards.
5. **Container limit set too low for real workload.** Memory is stable and
   healthy, but the ceiling was sized before traffic grew. Distinguishing signal:
   flat, non-growing usage sitting just under the threshold for weeks.

## Investigation

1. **Read the 24-hour shape before anything else.** On the Order Pipeline
   dashboard, plot `order_service_memory_bytes` over 24h and 7d. The curve
   distinguishes cause 2 (steady climb through traffic troughs) from cause 3
   (single step) from cause 4 (periodic spike) faster than any profiling will.
2. **Check restart counts.** `kubectl get pods -n ecommerce -l app=order-service`
   and read the `RESTARTS` column. Non-zero and climbing means OOM-kills are
   already happening and this is more urgent than `severity=medium` implies.
3. **Confirm OOM-kill rather than another exit.** `kubectl describe pod <pod> -n
   ecommerce` and look for `Last State: Terminated, Reason: OOMKilled`. A
   different reason means memory is a coincidence, not the cause.
4. **Correlate with deploys.** `kubectl rollout history deployment/order-service
   -n ecommerce`. A step change aligning with a rollout is cause 3.
5. **Check whether memory falls during the overnight trough.** This is the
   single most useful discriminator: a healthy service releases memory when
   traffic drops. One that does not is retaining objects: cause 1 or 2.
6. **Compare replicas.** If one pod is far above the others on the same traffic,
   suspect a leak triggered by a specific long-lived connection or a poisoned
   cache entry rather than a uniform workload cost.

## Resolution

**Immediate relief, any cause:** `kubectl rollout restart deployment/order-service
-n ecommerce`. This is a deliberate rolling restart: pods drain in-flight work
before terminating, unlike an OOM-kill, which does not. It buys hours to days.
It is not a fix, and a service that needs it on a schedule has an open bug.

**Unbounded cache (cause 1):** add an eviction policy: a size ceiling or a TTL.
Prefer bounding the cache to raising the limit; an unbounded cache will find
whatever ceiling you give it.

**Leak (cause 2):** capture a heap profile from an affected pod before
restarting it: the restart destroys the evidence. Compare object counts by type
against a freshly-started pod; the type that grows without bound names the
retaining code path.

**Deploy baseline (cause 3):** if the new baseline is legitimate, raise the
container memory limit and the alert threshold together. Raising only the
threshold hides the alert while leaving the OOM-kill in place, which is strictly
worse than doing nothing.

**Batch job (cause 4):** move the job out of the request-serving process, or
stream results instead of materialising them. Schedule it off-peak if neither is
possible.

**Undersized limit (cause 5):** raise the limit and the threshold to match real
usage, with headroom. Record why in the deploy manifest so the next person does
not read it as a leak.

## Escalation

Does not auto-page at `severity=medium`. Pick it up during working hours unless
the growth curve puts the container limit within a few hours, in which case
treat it as urgent and page the order-service on-call.

Escalate immediately if pods are actively OOM-killing: restarts drop in-flight
orders, which makes this a customer-facing problem regardless of the declared
severity.

Bring in the service owner if a heap profile is needed on production traffic.
Profiling adds overhead and should not be turned on unilaterally during peak
hours.

## Related

- `order-service-high-failure-rate`: same service, different failure mode, and
  the one to check first. That runbook covers orders failing while the process
  stays healthy; this one covers the process itself being unhealthy while orders
  still succeed. If both are firing, memory pressure is likely causing the
  failures via OOM-kills, so work this one.
- `checkout-timeout-rate`: GC pauses under memory pressure can surface upstream
  as checkout slowness before anything in order-service errors.
