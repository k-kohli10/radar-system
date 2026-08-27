# 📮 ADR 0003: Postgres Transactional Outbox for All Agent Communication

## Contents

- [Status](#status) · [Context](#context) · [Decision](#decision) · [Consequences](#consequences)
- [Why Not Kafka or NATS](#why-not-kafka-or-nats)
- [Why the Outbox Pattern Works Here](#why-the-outbox-pattern-works-here)
- [Tradeoffs Accepted](#tradeoffs-accepted)
- [Migration Path If You Outgrow This](#migration-path-if-you-outgrow-this)

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

## Why Not Kafka or NATS

Kafka is the right choice when you need high throughput, multiple independent
consumers per topic, long-term retention, or replay from any point in history.
RADAR has none of those requirements in v1: one pipeline, three agents, each
processing one event in sequence, at tens of incidents per hour.

| | What it costs you | Why RADAR doesn't need it |
|---|---|---|
| **Kafka** | Another stateful system (brokers, ZooKeeper/KRaft, topic configs), schema versioning, consumer group coordination, a separate failure domain, real local-dev overhead | No high-throughput, multi-consumer, or replay requirement at this scale |
| **NATS** | Still an external system to run and monitor; JetStream (for persistence) adds config; consumer-side idempotency is still required either way | "Simpler than Kafka" isn't the same as "simpler than the Postgres you already run" |

This is a solo project on a small self-hosted cluster. Either option would double
the operational surface area before a line of application code runs.

---

## Why the Outbox Pattern Works Here

The critical property is atomicity. When ingestion creates an incident, the outbox
event for the watcher must either both commit or both roll back. There is no world
where the incident exists but the watcher never gets triggered, or vice versa.

With an external broker you lose this guarantee unless you implement a two-phase
commit or an outbox pattern anyway. So you end up building the outbox pattern on top
of Kafka, which is strictly worse than just using the outbox pattern on Postgres.

The outbox pattern on Postgres gives you:
- Atomicity between state change and event, guaranteed by the database
- Zero additional infrastructure to operate
- Dead letter handling as a simple status column
- Full event history queryable with SQL
- Replay by updating `status` back to `pending`
- Debugging by reading rows in a table, not decoding binary log formats
- Idempotency via the `processed_events` table and `event_id`

---

## Tradeoffs Accepted

Real limitations, acceptable for v1:

| Tradeoff | Cost | Why it's acceptable now |
|---|---|---|
| **Single worker bottleneck** | If outbox-worker crashes, the pipeline stops until Kubernetes restarts it | No consumer-group redundancy needed yet |
| **Polling overhead** | 2s per hop; up to 6s across three agents, on top of LLM call time | End-to-end latency is measured in minutes, not milliseconds |
| **Postgres under load** | The outbox table becomes a hot write path at high volume | Not a concern at homelab scale; revisit past thousands of incidents/hour |
| **No fan-out** | One event goes to one target; multiple consumers need multiple rows | Not a v1 requirement |

---

## Migration Path If You Outgrow This

If RADAR scales to the point where the outbox pattern is a bottleneck (thousands of
incidents per hour, multiple consumers needed), the migration path is:

1. Keep the outbox table as the write side
2. Add a Kafka producer to the outbox-worker that publishes events to topics
3. Migrate consumers from HTTP endpoints to Kafka consumers incrementally
4. Remove the HTTP dispatch path once all consumers are on Kafka

The application code barely changes. The outbox-worker changes. Everything else stays.

This is why the pattern is a good choice even beyond v1: it does not paint you into
a corner.
