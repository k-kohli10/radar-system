# 🔄 ADR 0016: Incident Lifecycle State Machine

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap Kohli

---

## Contents

- [Context](#-context)
- [States](#-states)
- [State Definitions](#-state-definitions)
- [State Transition Diagram](#-state-transition-diagram)
- [Valid Transitions](#-valid-transitions)
- [Who Can Trigger Each Transition](#-who-can-trigger-each-transition)
- [Audit Log Entries Per Transition](#-audit-log-entries-per-transition)
- [Stale Incident Handling](#-stale-incident-handling)
- [Multiple Alerts on One Incident](#-multiple-alerts-on-one-incident)
- [Amendments (Phase 9: v0.9-feedback)](#-amendments-phase-9-v09-feedback)
- [Decision Record](#-decision-record)

---

## 🧭 Context

An incident in RADAR goes through several states from the moment the first alert
fires to the moment it is closed. Without a defined state machine, services write
whatever status they feel like, transitions happen inconsistently, and the Slack bot
returns confusing status information to engineers.

This document defines the state machine. Every service that touches an incident must
follow it.

---

## 🔢 States

```
open
investigating
resolved
closed
```

That is it. Four states. Not six. Not ten.

---

## 📖 State Definitions

### 🟢 open

The incident has been created. At least one alert has fired. The pipeline has not
yet produced a recommendation. This is the initial state.

An incident stays in `open` from the moment it is created until the reasoner agent
produces a recommendation (or a fallback recommendation). If no recommendation is
produced within 10 minutes, the `IncidentRCAStalled` Prometheus alert fires.

### 🔎 investigating

The reasoner agent has written a recommendation. The Slack card has been sent.
The incident is now in the hands of an on-call engineer.

Transition to `investigating` happens automatically when `recommendation.created`
outbox event is processed by feedback-service.

An incident can stay in `investigating` indefinitely. It transitions out when an
engineer takes action: either marking it resolved via Slack feedback or the source
alerts resolve.

### ✅ resolved

The incident has been addressed. Either:
- An engineer clicked "resolved" in the Slack card or bot command
- The original alert resolved in Prometheus and ingestion received a resolved payload

When an incident resolves, `resolved_at` is set. The incident may still have
open follow-up questions but the immediate operational impact is over.

### 🔒 closed

The incident has been reviewed, documented, and closed. In v1 this is a manual
action via the Slack bot (`@radar close INC-abc123`). In a future version this
could trigger a post-incident review workflow.

`closed_at` is set when transitioning to this state. Closed incidents do not appear
in `@radar open` results.

> **Amendment 3 (Phase 9): `closed_at` is RESERVED, not yet reached.** The
> `resolved -> closed` edge and its `closed_at` stamp exist in the shipped state
> machine (`transition_status` sets `closed_at` on that transition), but no Phase 9
> caller performs it: `@radar close` is deferred, so in practice `closed_at` stays
> NULL until that command lands. Recorded here so the column reads as deliberately
> reserved rather than silently orphaned. See Amendments (Phase 9) below.

---

## 🗺️ State Transition Diagram

```
                    alert fires
                        |
                        v
                     [ open ]
                        |
                        | reasoner writes recommendation
                        v
                  [ investigating ]
                        |
              __________|__________
             |                     |
             | engineer resolves   | alert resolves in Prometheus
             v                     v
           [ resolved ] <---------+
                |
                | engineer closes (manual, @radar close)
                v
            [ closed ]
```

---

## ↔️ Valid Transitions

| From | To | Allowed? | Note |
|---|---|---|---|
| open | investigating | Yes | recommendation written by reasoner |
| open | resolved | Yes | alert resolved before recommendation written |
| investigating | resolved | Yes | engineer marks resolved, or alert resolves |
| investigating | open | No | regression |
| resolved | closed | Yes | engineer closes |
| resolved | investigating | No | incident is resolved; open a new one |
| closed | any | No | closed is terminal |

If a service attempts an invalid transition, it must log an error, write to
`audit_log`, and reject the state change. It must not silently accept an invalid
transition.

---

## 👤 Who Can Trigger Each Transition

| Transition | Triggered by |
|---|---|
| open -> investigating | feedback-service (on `recommendation.created`; see Amendment 1) |
| open -> resolved | ingestion (alert resolved payload received) |
| investigating -> resolved | ingestion (last firing alert resolves) OR feedback-service (engineer Slack action); see Amendment 2 |
| resolved -> closed | feedback-service (Slack bot command `@radar close`) |

No other service changes incident status. This is enforced by the repository layer.
See **Amendments (Phase 9)** below for the two authority corrections above.

The `IncidentRepository.transition_status()` method validates the transition before
writing:

```python
VALID_TRANSITIONS = {
    "open": {"investigating", "resolved"},
    "investigating": {"resolved"},
    "resolved": {"closed"},
    "closed": set(),
}

async def transition_status(
    self,
    incident_id: UUID,
    new_status: str,
    actor: str,
    session: AsyncSession
) -> Incident:
    incident = await self.get(incident_id, session)
    valid_next = VALID_TRANSITIONS.get(incident.status, set())

    if new_status not in valid_next:
        raise InvalidStateTransitionError(
            f"Cannot transition {incident.status} -> {new_status} "
            f"for incident {incident_id}"
        )

    incident.status = new_status
    incident.updated_at = datetime.utcnow()

    if new_status == "resolved":
        incident.resolved_at = datetime.utcnow()
    if new_status == "closed":
        incident.closed_at = datetime.utcnow()

    await session.flush()
    return incident
```

---

## 📜 Audit Log Entries Per Transition

Every status transition writes to `audit_log`. No exceptions.

```
open -> investigating:
  event_type: incident.investigating
  actor: reasoner-agent
  payload: {recommendation_id, is_fallback, confidence}

open -> resolved (alert resolved):
  event_type: incident.resolved
  actor: ingestion
  payload: {resolved_by: "alert_resolution", alert_source}

investigating -> resolved (engineer):
  event_type: incident.resolved
  actor: slack_user_id
  payload: {resolved_by: "engineer", slack_user_id}

resolved -> closed:
  event_type: incident.closed
  actor: slack_user_id
  payload: {slack_user_id}

invalid transition attempt:
  event_type: incident.invalid_transition
  actor: <service that attempted it>
  payload: {from_status, attempted_status, reason: "invalid_transition"}
```

---

## ⏰ Stale Incident Handling

An incident that stays in `open` for more than 10 minutes without a recommendation
is considered stalled. This fires the `IncidentRCAStalled` Prometheus alert.

An incident that stays in `investigating` for more than 4 hours without a resolution
is considered stale. No automated action in v1. The Slack bot returns a warning
when queried:

```
@radar incident INC-abc123
-> [WARNING] This incident has been in investigating state for 6 hours.
   Last activity: recommendation written 6 hours ago.
```

In v2 a scheduled job could nag the on-call channel about stale incidents.

---

## 🔁 Multiple Alerts on One Incident

When a second alert with the same fingerprint arrives while an incident is `open`
or `investigating`, it attaches to the existing incident. The incident `alert_count`
increments. No state transition happens.

When all attached alerts resolve in Prometheus and ingestion receives resolved
payloads for all of them, ingestion transitions the incident to `resolved`
automatically.

Partial resolution (some alerts resolved, some still firing) does not change
incident status. The incident stays in its current state.

---

## 📝 Amendments (Phase 9: v0.9-feedback)

Recorded when `IncidentRepository.transition_status` was codified in
`packages/database`. The state machine itself (four states, four edges) is
unchanged; these correct the surrounding prose where it contradicted the shipped
enforcement, and are kept here rather than silently editing the original text.

**Amendment 1: feedback-service owns `open -> investigating`, not reasoner-agent.**
The "Who Can Trigger" table originally attributed this transition to reasoner-agent;
the State Definitions section (unchanged) already said it happens "when
`recommendation.created` is processed by feedback-service," so the ADR contradicted
itself. feedback-service is correct, on two grounds that agree. *Structural:* the
service that PROCESSES an event performs the write. `recommendation.created` is
emitted by the reasoner and consumed by feedback-service, and emitting an event does
not make you the actor for a transition triggered by consuming it. *Semantic:*
`investigating` must mean "a human has been told," which is true when the card is
DELIVERED, not when the recommendation row is written: if the reasoner transitioned,
an incident would sit in `investigating` while Slack delivery was still queued in the
outbox, or had dead-lettered.

**Amendment 2: ingestion's authority is `{open, investigating} -> resolved`.**
Originally ingestion was granted only `open -> resolved`. But an Alertmanager
`resolved` webhook usually arrives AFTER the RCA card has been sent, i.e. while the
incident is already `investigating` (the common case). Restricting ingestion to
`open -> resolved` left that common path unauthorized, describing a system that could
not handle its own primary flow. Ingestion may now resolve from either `open` or
`investigating`, actor `ingestion`, `resolved_by: "alert_resolution"`. Routing the
webhook through an outbox event to feedback-service was considered and rejected: it
makes incident resolution depend on a service that need not exist yet, inverting the
build order (ingestion-side lifecycle is proven BEFORE Slack).
This is consistent with "Multiple Alerts on One Incident" above, which already has
ingestion resolving the incident when its last firing alert resolves.

**Amendment 3: `closed_at` is reserved, not orphaned.** See the note under the
`closed` state definition. The edge and stamp ship; no Phase 9 caller reaches them.

**Amendment 4: the exception is named `InvalidStateTransitionError`.** The illustrative
code above originally wrote `InvalidStateTransition`; the shipped class carries the
`Error` suffix to match RADAR's error hierarchy (`ConflictError`, `NotFoundError`, …),
which the repo's ruff config (N818) enforces. It subclasses `ConflictError` (409): an
illegal transition is a conflict with current state, most often because the state moved
under the caller. The shipped `transition_status` signature also differs from the
sketch above: it takes the incident id (loaded under `SELECT ... FOR UPDATE`, not via
`get`), keyword-only `actor` / `correlation_id` / `audit_payload` / `occurred_at`,
writes the `audit_log` row for a valid transition in the caller's transaction, and
writes nothing on rejection (the caller records the `incident.invalid_transition`
attempt in its own transaction).

---

## ✔️ Decision Record

Four states. Defined valid transitions. Transition validation in the repository
layer, not in services. Every transition writes to audit_log. Invalid transitions
are logged and rejected, not silently accepted.
