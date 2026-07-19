---
runbook_id: inventory-cache-invalidation-storm
title: Inventory Cache Invalidation Storm
services:
  - inventory-service
severity: high
status: fixture
---

# Inventory Cache Invalidation Storm

## Summary

A large number of cached inventory entries became invalid at once, and the
resulting flood of concurrent misses is hitting the database far harder than
steady-state traffic ever does. The service is not receiving more requests than
usual — it is simply answering a much larger share of them the expensive way.

The distinguishing question against ordinary latency is **shape over time**: a
storm spikes and then decays as the cache refills, whereas a genuinely slow
service stays slow. A storm that does *not* decay is the more serious variant,
because something is invalidating continuously and the cache never gets to do
its job.

The worst form is a stampede: many concurrent requests missing on the *same*
key, each independently querying for an answer the others are already fetching.
That is self-inflicted load amplification, and it is fixed differently from
simple cold-cache recovery.

## Symptoms

- Cache hit rate dropping sharply from its normal level, which is the primary
  signal. Latency is the consequence; hit rate is the cause and moves first.
- `inventory_check_p95_seconds` elevated, often firing `InventoryCheckLatency`
  as a downstream effect.
- Database query volume spiking while request volume stays flat. That
  divergence — more queries for the same number of requests — is the storm's
  signature and separates it from a traffic increase.
- A spike-and-decay curve as the cache refills, typically over seconds to
  minutes. A flat elevated line instead means continuous invalidation.
- In a stampede, many simultaneous identical queries for the same key, visible
  as duplicate query patterns in database logs.

## Impact

Elevated latency for inventory checks, which propagates to checkout as slowness
and, if sustained, as timeouts. The impact is bounded by how quickly the cache
refills — a one-off storm is usually a blip measured in seconds.

The risk is disproportionate to the duration, because the database is absorbing
a load spike it is not provisioned for. A large enough storm can saturate the
database's connection capacity or its CPU, which turns a cache problem into a
service-wide one affecting every query, not just inventory lookups.

Recurring storms are worse than a single large one. Each is survivable, but a
pattern that repeats on a schedule means the cache is providing far less
protection than its hit rate suggests during the good periods.

## Likely Causes

1. **Mass simultaneous expiry.** A large batch of entries written together with
   an identical TTL expires together. Distinguishing signal: storms recurring at
   a fixed interval matching the TTL, and onset aligned with a prior bulk load.
   The most common cause and entirely preventable.
2. **Cache stampede on a hot key.** A popular product's entry expires and
   hundreds of concurrent requests miss simultaneously, each issuing its own
   query. Distinguishing signal: duplicate identical queries in a tight window,
   and impact concentrated on specific popular items.
3. **Cache restart or flush.** The cache was restarted, evicted wholesale, or
   deliberately flushed. Distinguishing signal: hit rate falling to near zero
   instantly rather than degrading, correlated with a deploy or maintenance.
4. **Continuous invalidation from an upstream loop.** A misbehaving writer, a
   deploy loop, or a sync job invalidating entries as fast as they are
   populated. Distinguishing signal: hit rate stays low and does not recover —
   the defining feature of the non-decaying variant.
5. **Cache key change on deploy.** A release changed the key format, so every
   lookup misses against a cache full of unreachable entries. Distinguishing
   signal: hit rate drops to near zero exactly at rollout and stays there while
   the cache refills under the new scheme.

## Investigation

1. **Read the hit-rate curve first, then its shape.** Spike-and-decay is a
   one-off refill (causes 1, 2, 3, 5); flat-and-low is continuous invalidation
   (cause 4). This split determines whether you wait or intervene, and it comes
   before any database work.
2. **Compare database query volume against request volume.** Queries rising
   while requests stay flat confirms a cache problem rather than a traffic
   problem. If both rose, you are in `inventory-check-latency` cause 5, not here.
3. **Check for duplicate concurrent queries on the same key.** This is what
   identifies a stampede specifically, and it changes the fix from capacity to
   coalescing.
4. **Correlate with deploys, cache restarts, and bulk jobs.** Cause 3 and cause
   5 both align exactly with a rollout; cause 1 aligns with a bulk load one TTL
   period earlier, which is easy to miss because the trigger is not recent.
5. **Check whether storms recur on a fixed interval.** A repeating period
   matching the TTL is conclusive for cause 1 and is visible only over a longer
   window than the current incident.
6. **Look for a continuous invalidator if hit rate is not recovering.** Identify
   what is writing to or invalidating the cache. Cause 4 will not resolve on its
   own and is the only variant that genuinely requires intervention now.

## Resolution

**Mass expiry (cause 1):** add jitter to TTLs so entries written together expire
apart. A random spread of ten to twenty percent around the base TTL is enough to
convert a cliff into a gentle slope. This is the single highest-value fix here
and it prevents recurrence rather than surviving it.

**Stampede (cause 2):** coalesce concurrent misses so only one request per key
queries the database while the others wait for that result. Serving a slightly
stale value while a refresh is in flight is usually preferable to amplifying
load, and for inventory availability a few seconds of staleness is acceptable
where a database saturation is not.

**Cache restart or flush (cause 3):** let it refill. Consider pre-warming
popular keys after a planned flush; an unplanned one is a lesson about how
quickly the service degrades without its cache.

**Continuous invalidation (cause 4):** find and stop the invalidator. Nothing
else helps while it is running, and adding cache capacity or database replicas
against it is wasted effort.

**Key change (cause 5):** this is expected behaviour for the change, not a
fault. Pre-warm the new keys before rollout next time, or roll out gradually so
the miss rate rises in increments the database can absorb.

**In all cases**, confirm recovery on cache hit rate rather than on latency.
Latency returns to normal as soon as load drops, which can happen for reasons
unrelated to the cache actually being repopulated.

## Escalation

Page the inventory-service on-call if the storm is not decaying, or if database
saturation is affecting queries beyond inventory. A decaying storm usually needs
no page — it resolves before anyone can act on it.

Escalate to the database on-call if the query spike is threatening connection
capacity or CPU. That is the path by which an inventory cache problem becomes
everyone's problem, and it deserves attention before it gets there.

Escalate as a recurring defect rather than an incident if storms repeat on a
schedule. Each individual occurrence is survivable, which is exactly why a
recurring pattern gets tolerated indefinitely unless someone raises it
deliberately.

## Related

- `inventory-check-latency` — the alert this usually fires, where a cache miss
  storm is listed as cause 3. That runbook covers inventory latency from any
  cause; this one covers the specific recurring pattern and its prevention. If
  hit rate is normal and latency is high, that runbook applies, not this one.
- `inventory-stock-reservation-leak` — a different inventory failure entirely.
  This one makes correct answers slow; that one makes fast answers wrong.
- `order-service-connection-pool-exhaustion` — a large enough storm can exhaust
  database connections, at which point that failure mode arrives on top of this
  one.
