# ADR 0003: Postgres Transactional Outbox for All Agent Communication

## Status
Accepted

## Context
The agent pipeline (watcher-agent → planner-agent → reasoner-agent) must hand work off
reliably: an incident created by watcher-agent must always result in a plan request
reaching planner-agent, exactly once, even across process restarts, network blips, or
concurrent instances of the same agent. A naive approach, write to Postgres, then
`POST` directly to the next agent's HTTP endpoint, has a well-known failure mode: the
write succeeds, the process crashes or the network call fails before the POST
completes, and the handoff is silently lost. Adding a message broker (Redis Streams,
RabbitMQ, Kafka) to close that gap means running and operating another stateful system,
plus a second source of truth to keep consistent with Postgres.

## Decision
Every cross-agent handoff is a row in the Postgres `outbox_events` table, inserted in
the **same transaction** as the state change it represents (e.g. inserting an
`incidents` row and its `outbox_events(incident.plan_requested)` row happen atomically).
A single dedicated `outbox-worker` service polls this table with
`SELECT ... FOR UPDATE SKIP LOCKED`, dispatches each event via `POST /events` to its
target service, and manages retry scheduling and dead-lettering. Agents never call each
other's HTTP endpoints directly to trigger a handoff. The one exception is
reasoner-agent's synchronous call to llm-gateway, which is a request/response call, not
a fire-and-forget event.

## Consequences
- "State changed but the event was never sent" and "event sent but the state change
  never committed" are both structurally impossible, since they're the same
  transaction.
- No message broker to run, operate, patch, or back up. Postgres, already the system of
  record, is the only stateful dependency in the write path. This is also why
  [ADR 0006](0006-no-redis.md) rules out Redis as a queue.
- `FOR UPDATE SKIP LOCKED` lets multiple outbox-worker replicas poll concurrently
  without double-claiming the same event, so horizontal scaling of the dispatcher
  comes for free.
- Every agent must independently implement idempotent processing (`processed_events`
  check before work, insert after), because outbox-worker's at-least-once delivery
  means an agent's `POST /events` handler can be called more than once for the same
  `event_id`.
- Latency is bounded by the outbox-worker's poll interval, not sub-millisecond like a
  push-based broker. Acceptable for an incident-response pipeline where end-to-end
  latency is measured in seconds, not microseconds.
