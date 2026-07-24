"""RCA card delivery: post to Slack, then record — at-least-once, one card.

This is the deep-treatment core of the feedback service. Delivering a card to
Slack is an external side effect with NO idempotency key (``chat.postMessage``
returns a fresh timestamp every call), so exactly-once is not on the menu. The
honest question is which failure to prefer, and the answer is deliberate:

    A DUPLICATE card is visible and annoying. A MISSED card means an incident
    nobody hears about — the exact failure this whole system exists to prevent.

So delivery is AT-LEAST-ONCE, post-then-record: the card is posted first, and only
once ``send`` returns is the delivery recorded. A crash between the post and the
record leaves ``slack_message_ts`` NULL, so redelivery retries and posts again —
a duplicate, which is the failure we chose. The alternative (record first, then
post) would be at-MOST-once: a failed post that already recorded would never
retry, and the card is lost on precisely the failures redelivery exists to handle.
:func:`deliver_rca` writes ``slack_message_ts`` strictly AFTER ``send`` returns;
never reorder that.

**One card under concurrency — the row lock, held across the Slack call.** The
recommendation row is taken ``SELECT ... FOR UPDATE`` and the lock is held THROUGH
the post. Two concurrent deliveries of one recommendation serialize: the winner
posts and sets ``slack_message_ts``; the loser blocks, then re-reads it set and
returns without posting. One card, no error path.

Holding a row lock across a network call is normally avoided in RADAR — the
reasoner explicitly does NOT hold a transaction across its LLM call. The reasoning
is the same; the facts differ, so the conclusion flips. The LLM call runs up to a
minute and every incident makes one, so a held lock there would pin connections
under load. A Slack post is sub-second with a 10s ceiling, and contention on ONE
recommendation row happens only on redelivery — rare, and bounded. Do not "fix"
this by copying the reasoner's release-before-call pattern: here the lock IS the
no-double-post guarantee, and releasing it before the post reopens the double-post
window the whole design closes.

``slack_message_ts`` being UNIQUE is cross-row integrity only — no two
recommendations may claim the same Slack message. It is NOT what prevents a double
post (two deliveries of one recommendation would post two DIFFERENT timestamps,
which never collide). The lock is.

**The incident becomes ``investigating`` here, and only here.** ADR 0016 Amendment
1 makes ``investigating`` mean "a human has been told", which is true when the card
is delivered — so the ``open -> investigating`` transition is done in this same
transaction, AFTER the post. It cannot commit without the card, so the incident
never sits in ``investigating`` while the RCA is still in the outbox or
dead-lettered. If stage 1 resolved the incident before the card arrived, the
transition is illegal and is caught and skipped — the card still delivers.
"""

from __future__ import annotations

from enum import StrEnum
from uuid import UUID

from radar_common import NotFoundError, RadarError, get_logger
from radar_contracts import NotificationBackend, RecommendedAction
from radar_database import (
    STATUS_INVESTIGATING,
    AuditLog,
    Incident,
    IncidentRepository,
    InvalidStateTransitionError,
    Recommendation,
    mark_processed,
)
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from radar_feedback_service.cards import RcaCardData, format_rca_card

log = get_logger("feedback.delivery")

NOTIFICATION_DELIVERED_EVENT = "notification.delivered"
"""Audit event recorded when an RCA card is delivered to Slack."""


class DeliveryOutcome(StrEnum):
    """What a delivery attempt did."""

    DELIVERED = "delivered"
    ALREADY_DELIVERED = "already_delivered"


class NotificationFailedError(RadarError):
    """The Slack post failed. Retryable — the card was not delivered, so redeliver.

    Wraps ANY failure from the notification backend without naming the vendor's
    exception type: nothing in ``apps/`` imports the Slack SDK, so the concrete
    error class stays behind the plugin boundary. The class name is logged (never
    the message, which can carry vendor detail) so an operator can tell a
    ``channel_not_found`` from a timeout without the app depending on the SDK.
    """


async def deliver_rca(
    session: AsyncSession,
    notifier: NotificationBackend,
    *,
    recommendation_id: UUID,
    incident_id: UUID,
    channel: str,
    event_id: UUID,
    service_name: str,
) -> DeliveryOutcome:
    """Deliver the RCA card for one recommendation, exactly as the module describes.

    Runs inside the caller's transaction and does NOT commit — the caller owns the
    boundary, so the ``slack_message_ts``, the audit row, and the processed marker
    land together or not at all. The recommendation row is locked ``FOR UPDATE``
    for the whole call, INCLUDING the Slack post (see the module docstring).

    Raises :class:`~radar_common.NotFoundError` if the recommendation or its
    incident is missing (permanent — the reasoner writes the recommendation in the
    same transaction as the event, so a missing one is corruption, not a race), and
    :class:`NotificationFailedError` if the post fails (retryable).
    """
    recommendation = (
        await session.execute(
            select(Recommendation)
            .where(Recommendation.id == recommendation_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if recommendation is None:
        raise NotFoundError(f"recommendation {recommendation_id} does not exist")

    if recommendation.slack_message_ts is not None:
        # Already delivered. Reached only by the loser of a concurrent race whose
        # gate check ran before the winner committed: the winner set the ts AND its
        # marker in one transaction, so both are present now. Nothing to post, and
        # nothing to mark (the winner's marker stands). Just return.
        log.info(
            "rca.already_delivered",
            recommendation_id=str(recommendation_id),
            incident_id=str(incident_id),
            slack_message_ts=recommendation.slack_message_ts,
        )
        return DeliveryOutcome.ALREADY_DELIVERED

    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise NotFoundError(f"incident {incident_id} does not exist")

    # Built from the ROWS, read now — current severity, status, root cause — never
    # from the event payload, which froze at emit time.
    text, blocks = format_rca_card(_card_data(recommendation, incident))

    # POST, lock held. Any failure here is a delivery failure: the transaction
    # rolls back (nothing recorded, ts stays NULL) and the worker redelivers.
    message_ts = await _post(notifier, channel=channel, text=text, blocks=blocks)

    # RECORD — strictly AFTER a successful post. This ordering IS the at-least-once
    # guarantee: writing the ts before the post would make a failed post look
    # delivered and never retry. See the module docstring.
    recommendation.slack_message_ts = message_ts
    session.add(_delivery_audit(recommendation, incident, message_ts, channel))
    await mark_processed(session, event_id, service_name)

    # NOW, and only now, the incident is `investigating`: a human has been told
    # (ADR 0016 Amendment 1). This transition is in the SAME transaction as the
    # delivery record and AFTER the post, so the incident cannot sit in
    # `investigating` while the card is still in the outbox or dead-lettered — the
    # exact lie the amendment exists to prevent. A failed post rolls this back with
    # everything else.
    await _mark_investigating(session, recommendation, incident)

    log.info(
        "rca.delivered",
        recommendation_id=str(recommendation_id),
        incident_id=str(incident_id),
        channel=channel,
        slack_message_ts=message_ts,
        is_fallback=recommendation.is_fallback,
    )
    return DeliveryOutcome.DELIVERED


async def _mark_investigating(
    session: AsyncSession,
    recommendation: Recommendation,
    incident: Incident,
) -> None:
    """Move the incident ``open -> investigating``, tolerating that it moved on.

    The incident was ``open`` when the reasoner wrote the recommendation, but stage
    1 can resolve it between then and delivery — an Alertmanager ``resolved`` webhook
    arriving while the RCA is still in the outbox. If so the incident is already
    ``resolved`` (terminal-ish) and ``resolved -> investigating`` is illegal:
    :meth:`transition_status` validates under the incident's own row lock and raises,
    which we CATCH and swallow. The card is still delivered — the RCA is worth
    reading even for an incident that just resolved — but we do not drag a resolved
    incident back to ``investigating``. Catching the error is right rather than
    checking status first: the check would be a TOCTOU race with a concurrent
    resolve, while the transition's ``FOR UPDATE`` makes the decision atomic.
    """
    try:
        await IncidentRepository(session).transition_status(
            incident.id,
            STATUS_INVESTIGATING,
            actor="feedback-service",
            correlation_id=recommendation.correlation_id,
            audit_payload={
                "recommendation_id": str(recommendation.id),
                "is_fallback": recommendation.is_fallback,
                "confidence": recommendation.confidence,
            },
        )
    except InvalidStateTransitionError:
        # The incident moved past `open` before the card was delivered (it
        # resolved). Deliver the card; leave the incident where it is.
        log.info(
            "incident.investigating_skipped",
            incident_id=str(incident.id),
            recommendation_id=str(recommendation.id),
            current_status=incident.status,
        )


async def _post(
    notifier: NotificationBackend,
    *,
    channel: str,
    text: str,
    blocks: list[dict[str, object]],
) -> str:
    """Post via the backend, translating ANY failure into a retryable RADAR error.

    The broad ``except`` is deliberate and tightly scoped to the single network
    call: every way a post can fail — Slack rejected it, a timeout, a missing
    timestamp — is the same outcome here, an undelivered card that must be retried.
    Catching the base ``Exception`` (not the SDK's error type) keeps the vendor
    import behind the plugin while still failing loud and retryable.
    """
    try:
        return await notifier.send(channel, text, blocks=blocks)
    except Exception as exc:
        # Class name only — a vendor message can carry detail we do not surface.
        raise NotificationFailedError(
            f"slack post failed: {type(exc).__name__}"
        ) from exc


def _card_data(recommendation: Recommendation, incident: Incident) -> RcaCardData:
    return RcaCardData(
        incident_id=incident.id,
        service_name=incident.service_name,
        title=incident.title,
        severity=incident.severity,
        status=incident.status,
        root_cause=recommendation.root_cause,
        confidence=recommendation.confidence,
        recommended_actions=[
            RecommendedAction(**action) for action in recommendation.recommended_actions
        ],
        is_fallback=recommendation.is_fallback,
    )


def _delivery_audit(
    recommendation: Recommendation,
    incident: Incident,
    message_ts: str,
    channel: str,
) -> AuditLog:
    """The delivery trail — the audit_log row alongside the slack_message_ts guard.

    The column is the functional record ("was this delivered?"); this row is the
    history ("we delivered X to channel Y as message Z, when"). Different jobs, both
    kept, written in the same transaction as the ts so the trail cannot claim a
    delivery the guard does not also hold.
    """
    return AuditLog(
        event_type=NOTIFICATION_DELIVERED_EVENT,
        entity_type="recommendation",
        entity_id=recommendation.id,
        correlation_id=recommendation.correlation_id,
        actor="feedback-service",
        payload={
            "incident_id": str(incident.id),
            "channel": channel,
            "slack_message_ts": message_ts,
            "is_fallback": recommendation.is_fallback,
        },
    )
