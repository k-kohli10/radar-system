"""What the watcher decides about an incident, and the event it emits.

Ingestion has already resolved which incident the alert landed on. What is left is the
judgement the watcher exists to make: **does this incident need an investigation, and
is it worse than we thought?**

    escalation (always)   -> raise severity if alerts are arriving fast enough
    new incident          -> emit incident.plan_requested, unless suppressed
    deduplicated alert    -> emit nothing (it is already planning)

The dedup branch emitting a second ``plan_requested`` would be a real bug, not a
harmless duplicate: the planner would build a second investigation plan for an
incident already downstream, the reasoner would burn a second LLM call on it, and the
engineer would get two RCA cards for one incident. The branch is on
``payload.deduplicated`` — a fact ingestion decided and stated, not something the
watcher re-derives — so the two cannot disagree about what "new" means. Escalation is
what that branch is *for*, and the reason duplicates are dispatched here at all.

Two rules, two ways to get them subtly wrong, both guarded:

- **Escalation only ratchets UP.** A rule whose ``escalate_to`` is milder than the
  incident's current severity must not write it — that would quietly *de*-escalate a
  critical incident because a later, milder alert arrived. Proven by trying to lower
  it, not just by watching it raise.
- **The escalation window is a rolling window.** The count is over alerts that arrived
  inside ``within_minutes``, not the incident's untimed ``alert_count`` — which would
  make ``within_minutes`` decorative and escalate three alerts spread over three hours
  exactly like three in ten seconds.

EVERYTHING IN ONE TRANSACTION
-----------------------------
The caller opens the session and commits once. The ``plan_requested`` outbox event,
the ``audit_log`` row, and the ``processed_events`` marker are therefore all-or-nothing
— the same rule as ingestion's incident/alert/outbox trio. A marker that committed
without its event would mean the incident silently never gets planned, and the
idempotency gate would ensure it never gets planned on redelivery either: the failure
would be permanent and completely silent. That is the failure this boundary exists to
make impossible.

CORRELATION ID
--------------
Every row written here carries the correlation id from the *envelope* — the value
minted at ingress and threaded through ingestion's incident, its alert, and the
``alert.normalized`` event that arrived. It is never re-minted. Phase 10's "trace the
whole pipeline by correlation_id alone" is only true if every stage passes the value
through rather than generating its own, and the only way that stays true is for the
tests to anchor on the original ingress UUID and assert equality — which they do.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any
from uuid import UUID

from radar_common import RadarError, get_logger, utcnow
from radar_contracts import AlertNormalizedPayload, PlanRequestedPayload, Severity
from radar_database import Alert, AuditLog, Incident, write_outbox_event
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from radar_watcher_agent.rules import CorrelationRules

log = get_logger("watcher.correlation")

PLAN_REQUESTED_EVENT = "incident.plan_requested"
"""Outbox event type the watcher emits to start an investigation."""

PLANNER_TARGET = "planner-agent"
"""Target service for the ``incident.plan_requested`` event."""

AUDIT_PLAN_REQUESTED = "watcher.plan_requested"
AUDIT_ALERT_ATTACHED = "watcher.alert_attached"
AUDIT_PLAN_SUPPRESSED = "watcher.plan_suppressed"
AUDIT_ESCALATED = "watcher.incident_escalated"

_SEVERITY_RANK: dict[Severity, int] = {s: i for i, s in enumerate(Severity)}
"""Rank by the Severity enum's DECLARATION order: CRITICAL=0 … INFO=4.

The enum's own docstring says rank-based comparison for escalation is the watcher's
concern and derives from that order — so it is derived, not restated. Note the
ordering is *inverted* against intuition: a LOWER number is MORE severe, because the
enum is declared most-severe first. Never use string comparison: ``"critical" <
"high"`` is true lexically and meaningless semantically.
"""


def is_more_severe(candidate: Severity, current: Severity) -> bool:
    """Is ``candidate`` strictly more severe than ``current``?

    The guard that makes escalation *escalation*. Without it, a rule with
    ``escalate_to: critical`` firing on an incident already at critical is harmless,
    but a rule with ``escalate_to: high`` firing on a critical incident would silently
    DE-escalate it — an incident quietly downgraded because a later, milder alert
    happened to arrive. Severity may only ever ratchet up.
    """
    return _SEVERITY_RANK[candidate] < _SEVERITY_RANK[current]


class IncidentNotFoundError(RadarError):
    """The incident named by the event payload does not exist.

    This should be impossible: ingestion writes the incident and the
    ``alert.normalized`` event in the SAME transaction, so an event that exists
    implies an incident that exists. If it happens, something deleted the incident
    out from under the pipeline, and the honest response is to fail loudly rather
    than invent one — the route maps it to 422, which the outbox worker treats as
    permanent and dead-letters, so it surfaces to a human instead of being retried
    forever against a row that is not coming back.
    """


@dataclass(frozen=True, slots=True)
class CorrelationOutcome:
    """What the watcher decided about one alert."""

    incident_id: UUID
    plan_requested: bool
    suppressed: bool = False
    escalated_to: Severity | None = None


async def correlate(
    session: AsyncSession,
    *,
    rules: CorrelationRules,
    correlation_id: UUID,
    payload: AlertNormalizedPayload,
) -> CorrelationOutcome:
    """Decide what this alert means for its incident, and write the consequences.

    Order matters. Escalation runs FIRST, so a severity it raises is the severity the
    plan carries — the planner and the engineer should see what the incident is now,
    not what the first alert happened to be. Then, for a new incident, suppression
    decides whether an investigation is worth starting at all.

    Adds rows but does NOT commit — the caller owns the transaction boundary, so the
    severity update, the outbox event, the audit rows, and the caller's
    ``processed_events`` marker are one atomic unit.
    """
    incident = await session.get(Incident, payload.incident_id)
    if incident is None:
        raise IncidentNotFoundError(
            f"incident {payload.incident_id} does not exist; ingestion writes the "
            "incident and the event in one transaction, so this row was deleted"
        )

    # Runs on BOTH branches, uniformly. On a new incident it simply cannot fire (one
    # alert, and every threshold is > 1), so there is no branch to get wrong — and no
    # temptation to "optimize" one away and thereby make a fresh burst unescalatable.
    escalated_to = await _escalate(
        session,
        rules=rules,
        incident=incident,
        correlation_id=correlation_id,
        payload=payload,
    )

    if payload.deduplicated:
        # Already planning. Emitting a second plan_requested here would re-plan an
        # incident that is already downstream — a duplicate RCA, and a second LLM
        # call, for one incident. Escalation above is what this branch is FOR.
        session.add(
            _audit(
                AUDIT_ALERT_ATTACHED,
                incident=incident,
                correlation_id=correlation_id,
                payload=payload,
            )
        )
        log.info(
            "alert.attached",
            incident_id=str(incident.id),
            service_name=payload.service_name,
            alert_name=payload.alert_name,
            alert_count=incident.alert_count,
        )
        return CorrelationOutcome(
            incident_id=incident.id, plan_requested=False, escalated_to=escalated_to
        )

    suppressed_by = await _suppressed_by(
        session, rules=rules, incident=incident, payload=payload
    )
    if suppressed_by is not None:
        # A new incident, but too soon after the last one of the same alert to be worth
        # investigating again. The incident EXISTS and its alert is recorded — an
        # engineer still sees it — but the pipeline does not spend a plan, an LLM call,
        # and a Slack card re-investigating a flapping alert it just looked at.
        session.add(
            _audit(
                AUDIT_PLAN_SUPPRESSED,
                incident=incident,
                correlation_id=correlation_id,
                payload=payload,
                extra={"suppress_follow_on_minutes": suppressed_by},
            )
        )
        log.info(
            "incident.plan_suppressed",
            incident_id=str(incident.id),
            alert_name=payload.alert_name,
            suppress_follow_on_minutes=suppressed_by,
        )
        return CorrelationOutcome(
            incident_id=incident.id,
            plan_requested=False,
            suppressed=True,
            escalated_to=escalated_to,
        )

    await _request_plan(
        session, incident=incident, correlation_id=correlation_id, payload=payload
    )
    session.add(
        _audit(
            AUDIT_PLAN_REQUESTED,
            incident=incident,
            correlation_id=correlation_id,
            payload=payload,
        )
    )
    log.info(
        "incident.plan_requested",
        incident_id=str(incident.id),
        service_name=payload.service_name,
        alert_name=payload.alert_name,
        severity=incident.severity,
    )
    return CorrelationOutcome(
        incident_id=incident.id, plan_requested=True, escalated_to=escalated_to
    )


async def _escalate(
    session: AsyncSession,
    *,
    rules: CorrelationRules,
    incident: Incident,
    correlation_id: UUID,
    payload: AlertNormalizedPayload,
) -> Severity | None:
    """Raise the incident's severity if alerts are arriving fast enough. Upward only.

    The count is over the ALERTS attached to this incident whose ``received_at`` falls
    inside the rule's window, ending at this alert's own arrival — a genuine rolling
    window. It is deliberately NOT ``incident.alert_count``, which is an untimed
    running total: using that would make ``within_minutes`` decorative, and three
    alerts spread over three hours would escalate exactly like three in ten seconds.

    ``received_at`` (when RADAR got the alert) rather than ``fired_at`` (when the
    detector says it fired): the rule is about arrival rate, and a detector backfilling
    old timestamps must not be able to manufacture a burst that never happened.

    Returns the severity escalated TO, or ``None`` if nothing fired.
    """
    if not rules.escalation:
        return None

    current = Severity(incident.severity)
    reference = payload.received_at

    # Every rule whose burst condition is met; the most severe target wins. With one
    # rule configured this is just "did it fire" — but the config is a list, and a
    # first-match-wins reading would make a stricter later rule unreachable.
    fired: list[Severity] = []
    for rule in rules.escalation:
        since = reference - timedelta(minutes=rule.within_minutes)
        count = await _alerts_received_since(session, incident.id, since=since)
        if count >= rule.alert_count_threshold:
            fired.append(rule.escalate_to)

    if not fired:
        return None
    target = min(fired, key=lambda s: _SEVERITY_RANK[s])

    # THE GUARD. Only ever upward. Without it, a rule whose escalate_to is milder than
    # the incident's current severity would DE-escalate it — a critical incident
    # silently downgraded because a later alert arrived at a lower severity.
    if not is_more_severe(target, current):
        return None

    incident.severity = target.value
    incident.updated_at = utcnow()
    session.add(
        _audit(
            AUDIT_ESCALATED,
            incident=incident,
            correlation_id=correlation_id,
            payload=payload,
            extra={"escalated_from": current.value, "escalated_to": target.value},
        )
    )
    log.info(
        "incident.escalated",
        incident_id=str(incident.id),
        escalated_from=current.value,
        escalated_to=target.value,
    )
    return target


async def _alerts_received_since(
    session: AsyncSession, incident_id: UUID, *, since: datetime
) -> int:
    """How many alerts on this incident arrived at or after ``since``."""
    count = await session.scalar(
        select(func.count())
        .select_from(Alert)
        .where(Alert.incident_id == incident_id, Alert.received_at >= since)
    )
    return int(count or 0)


async def _suppressed_by(
    session: AsyncSession,
    *,
    rules: CorrelationRules,
    incident: Incident,
    payload: AlertNormalizedPayload,
) -> int | None:
    """Is this new incident a follow-on too soon after the last one of the same alert?

    Returns the cooldown that suppressed it, or ``None`` if it should be planned.

    Scoped to ``(service_name, alert_name)``, not to the fingerprint: the fingerprint
    embeds severity, so a flapping alert that fires at ``high`` and then at ``critical``
    would slip past a fingerprint-scoped cooldown and be investigated twice — which is
    exactly the flapping the rule exists to damp.

    ``alert_name`` is not a column on ``incidents``, so the prior incident is found by
    joining the alerts that landed on it. Measured from the previous incident's
    ``opened_at`` to this one's, and the boundary is INCLUSIVE (a follow-on at exactly
    the cooldown is still suppressed) — the same convention as ingestion's dedup window,
    because two adjacent windows with opposite conventions is a bug waiting to be filed.
    """
    rule = rules.suppression_for(payload.alert_name)
    if rule is None:
        return None  # unlisted alert: never suppressed. This is the off-switch.

    cutoff = incident.opened_at - timedelta(minutes=rule.suppress_follow_on_minutes)
    previous = await session.scalar(
        select(Incident.id)
        .join(Alert, Alert.incident_id == Incident.id)
        .where(
            Alert.service_name == payload.service_name,
            Alert.alert_name == payload.alert_name,
            Incident.id != incident.id,
            Incident.opened_at >= cutoff,
            Incident.opened_at <= incident.opened_at,
        )
        .order_by(Incident.opened_at.desc())
        .limit(1)
    )
    return rule.suppress_follow_on_minutes if previous is not None else None


async def _request_plan(
    session: AsyncSession,
    *,
    incident: Incident,
    correlation_id: UUID,
    payload: AlertNormalizedPayload,
) -> None:
    """Add the ``incident.plan_requested`` outbox event (no commit).

    ``severity`` is read from the INCIDENT, not the alert: escalation may have raised
    it, and the planner and the engineer should see the incident's severity now, not
    the severity of whichever alert happened to trigger the request.

    ``alert_name`` comes from the alert, because the ``incidents`` table has no such
    column — and the planner matches its template on ``service_name:alert_name``.
    """
    body = PlanRequestedPayload(
        incident_id=incident.id,
        service_name=incident.service_name,
        alert_name=payload.alert_name,
        severity=Severity(incident.severity),
        alert_count=incident.alert_count,
    )
    await write_outbox_event(
        session,
        event_type=PLAN_REQUESTED_EVENT,
        target_service=PLANNER_TARGET,
        payload=body.model_dump(mode="json"),
        # The ingress value, passed through — never a fresh UUID. This is the link
        # in the chain Phase 10 traces by.
        correlation_id=correlation_id,
    )


def _audit(
    event_type: str,
    *,
    incident: Incident,
    correlation_id: UUID,
    payload: AlertNormalizedPayload,
    extra: dict[str, Any] | None = None,
) -> AuditLog:
    """Build the append-only audit record for what the watcher just decided.

    Every decision the watcher makes leaves one of these — including the decisions to
    do *nothing* (suppressed, attached). A silent no-op is indistinguishable from a bug
    when an engineer asks why their incident never got an RCA; an audit row answers it.
    """
    body: dict[str, Any] = {
        "alert_id": str(payload.id),
        "service_name": payload.service_name,
        "alert_name": payload.alert_name,
        "severity": incident.severity,
        "alert_count": incident.alert_count,
        "deduplicated": payload.deduplicated,
    }
    if extra:
        body.update(extra)
    return AuditLog(
        event_type=event_type,
        entity_type="incident",
        entity_id=incident.id,
        correlation_id=correlation_id,
        actor="watcher-agent",
        payload=body,
    )
