# 💀 ADR 0017: Dead Letter Replay Strategy

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap Kohli

---

## Contents

- [Context](#context)
- [What Causes Dead Letters](#what-causes-dead-letters)
- [Dead Letter Detection](#dead-letter-detection)
- [Investigation Before Replay](#investigation-before-replay)
- [Replay Mechanism](#replay-mechanism)
- [Replay Rules](#replay-rules)
- [When to Discard Instead of Replay](#when-to-discard-instead-of-replay)
- [Dead Letter Metrics and Alerting](#dead-letter-metrics-and-alerting)
- [Preventing Dead Letters](#preventing-dead-letters)
- [Decision Record](#decision-record)

---

## Context

The outbox worker retries failed event dispatches up to five times with exponential
backoff. After five failures, an event moves to `dead_letter` status and stops being
retried. At this point the incident pipeline is stalled for that event. An on-call
engineer or operator needs to decide what to do.

This document defines what happens to dead-letter events, how to investigate them,
how to replay them, and when to discard them instead.

---

## What Causes Dead Letters

An event reaches dead_letter status because the target service rejected or failed
to process it five consecutive times.

| Cause | Resolution |
|---|---|
| **Target service is down.** The pod crashed and Kubernetes has not restarted it yet, or the pod is in a crash loop. The event fails because the HTTP call cannot connect | Fix the service, then replay the event |
| **Target service returned a 5xx.** The service is up but something inside it is failing: a database connection problem, a dependency timeout, a bug. The outbox worker retries on 5xx but eventually gives up | Fix the root cause, then replay |
| **Target service returned a 4xx.** A 422 means the payload was invalid; a 401 means the token was wrong. These are not transient failures. Retrying the same invalid payload will always fail | Investigate the payload, fix the schema or the service, then decide whether to replay or discard |
| **Schema version mismatch.** The target service does not know how to parse the event's schema_version. This happens when a producer was updated but the consumer was not | Update the consumer, then replay |
| **Target service processed the event but crashed before acknowledging it.** The event retries and the service sees it again | Nothing to do: idempotency handles it. The service checks `processed_events` and returns 200 without redoing the work, and the outbox worker marks the event delivered. No dead letter |

---

## Dead Letter Detection

Prometheus alert:

```yaml
- alert: OutboxDeadLetterHigh
  expr: increase(radar_outbox_dead_letter_total[10m]) > 5
  annotations:
    summary: "Dead letter queue growing. Check outbox worker logs."
```

This fires if more than 5 events dead-letter in a 10-minute window. A single
dead-letter event does not fire the alert. A sustained rate does.

> **Corrected (Phase 9):** the paragraph originally here described `@radar status`
> reporting a dead-letter count and an `@radar dead-letters` command listing them.
> Neither shipped. `@radar status` reports open incidents, last RCA, and outbox
> depth only (`bot.py`'s `_run_status`). No dead-letter count. There is no
> `@radar dead-letters` verb in `BotCommandType`. The real v1 way to inspect and
> replay dead letters is the admin HTTP endpoints (`curl`) under
> [Replay Mechanism](#replay-mechanism) below. A Slack-native dead-letter view is
> unbuilt, same status as `@radar replay` further down that section.

---

## Investigation Before Replay

Do not replay blindly. Before replaying any dead-letter event:

**Step 1: Read the last_error.**
The `outbox_events.last_error` column stores the last error message from the target
service. This tells you whether the failure was transient (connection refused, 503)
or structural (422, schema mismatch).

**Step 2: Check the target service.**
Is it running? `kubectl get pods -n radar`. Are there errors in its logs?
`kubectl logs -l app=<service> -n radar --since=1h | grep ERROR`.

**Step 3: Check the payload.**
Read the event payload from the `outbox_events` table. Does it look valid? Does it
match the current schema for that event_type?

```sql
SELECT event_id, event_type, payload, last_error, attempts
FROM outbox_events
WHERE status = 'dead_letter'
ORDER BY created_at DESC;
```

**Step 4: Check if the downstream work was already done.**
Before replaying `incident.plan_requested`, check whether a plan already exists
for the incident. Before replaying `recommendation.created`, check whether the
feedback-service already sent the Slack message. Replaying an already-completed
event is safe because of idempotency, but it is worth knowing.

---

## Replay Mechanism

The outbox worker exposes two admin endpoints:

```
GET  /admin/dead-letter
     Returns all dead-letter events with id, event_type, target_service,
     last_error, attempts, created_at, payload.

POST /admin/dead-letter/{event_id}/requeue
     Moves the event back to pending status.
     Resets attempts to 0.
     Sets process_after to NOW().
     The outbox worker picks it up on the next poll cycle.
```

Replay via the Slack bot (v1 is manual via kubectl port-forward or internal tooling):

```bash
# Port-forward to outbox-worker
kubectl port-forward svc/outbox-worker 8080:8080 -n radar

# List dead letters
curl http://localhost:8080/admin/dead-letter

# Replay a specific event
curl -X POST http://localhost:8080/admin/dead-letter/<event_id>/requeue
```

In v2 the Slack bot can expose `@radar replay <event_id>` as a command.

---

## Replay Rules

**Rule 1: Fix the root cause before replaying.**
Replaying an event into a broken service will just dead-letter it again. Fix the
service first. Then replay.

**Rule 2: Replay individual events, not batches.**
Do not requeue all dead-letter events at once. Replay one, verify it processes
successfully, then replay the next. A batch replay into a still-broken service
creates a second wave of dead letters.

**Rule 3: 4xx failures usually mean discard, not replay.**
If an event died with a 422, the payload is invalid. Replaying it will fail again
with the same 422. Either the service has a bug that needs fixing first, or the
event should be discarded. Investigate before deciding.

**Rule 4: Replayed events still go through idempotency checks.**
The target service checks `processed_events` for the event_id. If the event was
partially processed before it died, the service will detect that and return 200
without re-doing the work. This is correct behavior. Do not remove idempotency
checks to make replays easier.

**Rule 5: Write to audit_log when replaying.**
Every manual requeue must write an `outbox.event_requeued` entry to audit_log
with: event_id, event_type, operator (who did it), reason (why).

---

## When to Discard Instead of Replay

Discard a dead-letter event when:

**The incident is already resolved.** If `incident.plan_requested` dead-lettered
but the incident is already `resolved` or `closed`, there is no point generating
a plan. Discard the event and close the dead-letter entry with reason `stale`.

**The payload is invalid and cannot be fixed.** If the event carries a malformed
payload that no version of the consumer can parse, and there is no way to reconstruct
the correct payload, discard it. Log the discard to audit_log with reason
`invalid_payload_unrecoverable`.

**The event is older than 24 hours.** A `recommendation.created` event that has
been sitting in dead-letter for 24 hours means the Slack notification is 24 hours
late. Sending it now is worse than not sending it. Discard it, mark the incident
as needing manual review.

Discard via a dedicated endpoint:

```
POST /admin/dead-letter/{event_id}/discard
     Body: {"reason": "stale|invalid_payload_unrecoverable|too_old"}
     Sets status to 'discarded'.
     Writes to audit_log with reason.
     Does not delete the row (audit trail must be preserved).
```

---

## Dead Letter Metrics and Alerting

| Metric | Description |
|---|---|
| `radar_outbox_dead_letter_total` | Counter. Increments each time an event dead-letters |
| `radar_outbox_dead_letter_depth` | Gauge. Current count of dead_letter status events |
| `radar_outbox_replays_total` | Counter. Increments each time an event is requeued |
| `radar_outbox_discards_total{reason}` | Counter. Increments each time an event is discarded |

The `dead_letter_depth` gauge should trend toward zero during normal operations.
If it grows consistently, something structural is broken.

---

## Preventing Dead Letters

Most dead letters are preventable:

**Liveness and readiness probes** ensure the outbox worker only dispatches to
services that are actually ready. If a service's readiness probe fails, Kubernetes
removes it from the service endpoint list and the outbox worker gets connection
refused immediately rather than after a timeout.

**Circuit breaker on the outbox worker** (future improvement) would detect sustained
failures to a specific target service and pause dispatching to it for a backoff
period, rather than burning through all retry attempts.

**Schema validation in the producer** catches invalid payloads before they are
written to the outbox. If ingestion tries to write an `alert.normalized` event with
a missing required field, the validation should fail at write time, not at dispatch
time five retries later.

---

## Decision Record

Dead letters are investigated before replay. Root cause fixed first. Individual
replay, not batch. 4xx events investigated carefully before replay. Stale or
unrecoverable events discarded with audit trail. All replays and discards logged.
Metrics track dead letter depth and rate.
