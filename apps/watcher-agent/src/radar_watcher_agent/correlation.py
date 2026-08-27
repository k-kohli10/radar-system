"""What the watcher decides about an incident, and the event it emits.

Ingestion has already resolved which incident the alert landed on. The watcher decides
whether the incident needs an investigation, and whether it is worse than we thought:

    escalation (always)   -> raise severity if alerts are arriving fast enough
    new incident          -> emit incident.plan_requested, unless suppressed
    deduplicated alert    -> emit nothing (it is already planning)

The new-vs-duplicate branch reads ``payload.deduplicated``, the fact ingestion decided
and stated, so the two stages cannot disagree about what "new" means. A second
``plan_requested`` on the dedup branch would re-plan an incident already downstream: a
second LLM call and a second RCA card for one incident.

Two rules, two ways to get them subtly wrong, both guarded:

- **Escalation only ratchets UP.** A rule whose ``escalate_to`` is milder than the
  incident's current severity must not write it, or a critical incident is quietly
  downgraded by a later, milder alert.
- **The escalation window is a rolling window.** The count is over alerts that arrived
  inside ``within_minutes``, not the incident's untimed ``alert_count``, which would
  escalate three alerts spread over three hours exactly like three in ten seconds.

Transaction boundary: the caller opens the session and commits once, so the
``plan_requested`` outbox event, the ``audit_log`` row, and the ``processed_events``
marker are all-or-nothing. A marker committed without its event would mean the incident
never gets planned, and the idempotency gate would keep it that way on redelivery: a
permanent, silent failure.

Every row written here carries the correlation id from the envelope, minted at ingress
and threaded through the whole pipeline. It is never re-minted, which is what makes
Phase 10's trace-by-correlation_id true.
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

The ordering is inverted against intuition: a LOWER number is MORE severe, because the
enum is declared most-severe first. Never use string comparison: ``"critical" <
"high"`` is true lexically and meaningless semantically.
"""


def is_more_severe(candidate: Severity, current: Severity) -> bool:
    """Is ``candidate`` strictly more severe than ``current``?

    The guard that makes escalation *escalation*: without it, a rule with
    ``escalate_to: high`` firing on a critical incident would silently DE-escalate it.
    Severity may only ever ratchet up.
    """
    return _SEVERITY_RANK[candidate] < _SEVERITY_RANK[current]


class IncidentNotFoundError(RadarError):
    """The incident named by the event payload does not exist.

    Ingestion writes the incident and the ``alert.normalized`` event in the SAME
    transaction, so this means the row was deleted out from under the pipeline. The
    route maps it to 422, which the outbox worker treats as permanent and
    dead-letters, surfacing it to a human instead of retrying forever.
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
    plan carries. Then, for a new incident, suppression decides whether an
    investigation is worth starting at all.

    Adds rows but does NOT commit: the caller owns the transaction boundary, so the
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
    # alert, and every threshold is > 1), so there is no branch to get wrong.
    escalated_to = await _escalate(
        session,
        rules=rules,
        incident=incident,
        correlation_id=correlation_id,
        payload=payload,
    )

    if payload.deduplicated:
        # Already planning. A second plan_requested here would mean a duplicate RCA
        # and a second LLM call for one incident. Escalation above is what this
        # branch is FOR.
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
        # Too soon after the last incident of the same alert to be worth investigating
        # again. The incident and its alert are still recorded, so an engineer sees it;
        # the pipeline just does not spend a plan, an LLM call, and a Slack card on a
        # flapping alert it just looked at.
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
    inside the rule's window, ending at this alert's own arrival: a genuine rolling
    window. Not ``incident.alert_count``, an untimed running total that would escalate
    three alerts spread over three hours exactly like three in ten seconds.

    ``received_at`` (when RADAR got the alert) rather than ``fired_at`` (when the
    detector says it fired): the rule is about arrival rate, and a detector backfilling
    old timestamps must not be able to manufacture a burst that never happened.

    Returns the severity escalated TO, or ``None`` if nothing fired.
    """
    if not rules.escalation:
        return None

    current = Severity(incident.severity)
    reference = payload.received_at

    # Every rule whose burst condition is met; the most severe target wins. The config
    # is a list, and first-match-wins would make a stricter later rule unreachable.
    fired: list[Severity] = []
    for rule in rules.escalation:
        since = reference - timedelta(minutes=rule.within_minutes)
        count = await _alerts_received_since(session, incident.id, since=since)
        if count >= rule.alert_count_threshold:
            fired.append(rule.escalate_to)

    if not fired:
        return None
    target = min(fired, key=lambda s: _SEVERITY_RANK[s])

    # THE GUARD. Only ever upward: a rule whose escalate_to is milder than the
    # incident's current severity must not silently downgrade it.
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
    would slip past a fingerprint-scoped cooldown and be investigated twice.

    ``alert_name`` is not a column on ``incidents``, so the prior incident is found by
    joining the alerts that landed on it. Measured from the previous incident's
    ``opened_at`` to this one's, with an INCLUSIVE boundary (a follow-on at exactly the
    cooldown is still suppressed), matching ingestion's dedup window convention.
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

    The payload carries what the planner needs to match its template and nothing more.
    It omits severity and alert_count: those are mutable incident state owned by the
    ``incidents`` row, and an event payload is frozen when written. An incident planned
    at ``high`` and escalated to ``critical`` a second later would carry ``high`` here
    forever, so nothing downstream may read it from here. See PlanRequestedPayload.

    ``alert_name`` comes from the alert, because the ``incidents`` table has no such
    column; the watcher is the last stage that has it.
    """
    body = PlanRequestedPayload(
        incident_id=incident.id,
        service_name=incident.service_name,
        alert_name=payload.alert_name,
    )
    await write_outbox_event(
        session,
        event_type=PLAN_REQUESTED_EVENT,
        target_service=PLANNER_TARGET,
        payload=body.model_dump(mode="json"),
        # The ingress value, passed through, never a fresh UUID: this is the link in
        # the chain Phase 10 traces by.
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

    Every decision leaves one of these, including the decisions to do *nothing*
    (suppressed, attached). A silent no-op is indistinguishable from a bug when an
    engineer asks why their incident never got an RCA; an audit row answers it.
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
