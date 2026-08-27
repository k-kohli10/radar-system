# 📮 radar-outbox-worker

The transport that moves work between RADAR agents.

RADAR services never call each other over HTTP directly. A producer writes an
`outbox_events` row in the same transaction as its state change; this worker
delivers it. It polls `outbox_events`, claims due `pending` rows with
`FOR UPDATE SKIP LOCKED` (so two workers never claim the same event), and
dispatches each via `POST /events` to its `target_service`, authenticating
outbound as the worker with `X-Radar-Agent-Token`.

Delivery is at-least-once with idempotent consumers: **claim → commit → dispatch
→ mark**, bounded backoff retry on failure, dead-letter promotion (with an
`audit_log` record) after the final attempt, and a reaper that recovers events
stranded in `processing` by a crashed worker.

It is a consumer, not an agent endpoint: there is **no** `POST /events` of its
own. It exposes `/healthz`, `/readyz`, `/metrics`, and token-guarded dead-letter
admin endpoints (list + requeue).
