# ADR 0006: No Redis

## Status
Accepted

## Context
Redis is a common default for queues, caches, and rate limiting, and would be a
plausible fit for several things RADAR needs: a job queue between agents, a cache in
front of Postgres, or a token bucket for rate limiting. RADAR already has a
transactional outbox in Postgres for agent handoffs (see
[ADR 0003](0003-postgres-outbox.md)), and a small self-hosted deployment favors
fewer stateful systems.

## Decision
Redis is not part of this architecture, anywhere. No queue, no cache, no rate limiter,
no session store. Where a queue is needed, use the Postgres outbox. Where a cache might
help, measure first. Postgres with correct indexes has, so far, been fast enough for
every read path in the plan (outbox polling, bot queries, retrieval pre-filtering).

## Consequences
- One fewer stateful system to deploy, back up, monitor, and reason about failure modes
  for, in a resource-constrained deployment.
- No cache-invalidation class of bugs, because there is no cache.
- If a genuine performance bottleneck emerges that Postgres indexing can't solve, this
  ADR is the place to record the decision to revisit, not a silent addition of Redis
  mid-implementation.
- Idempotency and locking rely on Postgres primitives
  (`processed_events` table, `SELECT ... FOR UPDATE SKIP LOCKED`) instead of
  Redis-based locks. That's slightly more ceremony per call site, but one less moving
  part in production.
