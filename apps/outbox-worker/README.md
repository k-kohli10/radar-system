# 📮 radar-outbox-worker

The transport that moves work between RADAR agents.

RADAR services never call each other over HTTP directly. A producer writes an
`outbox_events` row in the same transaction as its state change, and this
worker delivers it.

## Contents

- [Endpoints](#-endpoints)
- [Delivery pipeline](#-delivery-pipeline)

## 🔗 Endpoints

```
GET  /healthz                                process liveness
GET  /readyz                                 DB reachable
GET  /metrics                                Prometheus text format
GET  /admin/dead-letters                     agent-token guarded: list dead-lettered events
POST /admin/dead-letters/{event_id}/requeue  agent-token guarded: requeue one dead-lettered event
```

It is a consumer, not an agent endpoint: there is no `POST /events` of its own.

## 🔁 Delivery pipeline

1. Poll `outbox_events` and claim due `pending` rows with `FOR UPDATE SKIP
   LOCKED`, so two workers never claim the same event.
2. Commit the claim.
3. Dispatch via `POST /events` to the row's `target_service`, authenticating
   outbound as the worker with `X-Radar-Agent-Token`.
4. Mark the row delivered.

Delivery is at-least-once with idempotent consumers. A failed dispatch retries
with bounded backoff; after the final attempt the event is promoted to
dead-letter with an `audit_log` record. A reaper recovers events stranded in
`processing` by a crashed worker.
