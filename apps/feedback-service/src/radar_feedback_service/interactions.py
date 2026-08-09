"""Apply a parsed card interaction to the database, then reflect it on the card.

The handler the pure :mod:`callbacks` parser feeds. :func:`parse_callback` turned
Slack's echoed ``(action_id, value)`` into a validated :class:`ParsedCallback`; this
module is what that validated intent DOES. Two intents, one entry point:

- **👍 / 👎** (``feedback.up`` / ``feedback.down``) writes one ``feedback`` row against
  the recommendation the callback names — sentiment ``helpful`` / ``not_helpful``,
  stamped with the acting Slack user. Provider and model alias are copied from the
  recommendation being rated so downstream quality analysis can group feedback by
  model without a join back to ``recommendations``.
- **Resolve** (``incident.resolve``) moves the incident the recommendation belongs to
  ``-> resolved`` through the one validated entry point, :meth:`transition_status`,
  which locks the incident row, checks the edge, and writes the ``incident.resolved``
  audit — the same path every status change in RADAR takes (ADR 0016). There is no
  incident id on the callback by design; it is derived from the recommendation, so a
  resolve can never target a mismatched incident.

**The resolve loser path.** Two engineers can click Resolve on the same card, or one
can click it on an incident stage 1 already resolved from an Alertmanager webhook. The
second attempt is ``resolved -> resolved``, which :meth:`transition_status` rejects by
raising :class:`InvalidStateTransitionError` having written nothing. That is not an
error to surface — the incident IS resolved, which is what the click asked for — so it
is a benign :attr:`CallbackOutcome.ALREADY_RESOLVED`. But the rejected ATTEMPT is
forensic and the executor deliberately does not record it (recording the attempt is the
caller's job — ADR 0016, :data:`INVALID_TRANSITION_AUDIT_EVENT`), so this handler writes
the ``incident.invalid_transition`` row itself. Safe in the same transaction: the
executor raised BEFORE it flushed anything, so nothing of the winner's is entangled.

**Card reflection is best-effort, and strictly after the commit.** The database row is
the truth; the card footer only mirrors it. So the ``feedback`` row / the resolution
commits FIRST, and only then is the card re-rendered in place (``chat.update``) with a
one-line acknowledgement. A failed update is logged and swallowed — the feedback is
recorded whether or not the mirror caught up, and losing a footer edit must never roll
back a recorded rating or a real resolution. This is the reverse of RCA delivery, where
the Slack post IS the delivery and the row follows it; here the row is the point and the
card only reflects it.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from enum import StrEnum
from typing import Protocol
from uuid import UUID

from radar_common import NotFoundError, get_logger
from radar_contracts import BotMention, NotificationBackend, NotificationInteraction
from radar_database import (
    INVALID_TRANSITION_AUDIT_EVENT,
    STATUS_RESOLVED,
    AuditLog,
    Database,
    Feedback,
    Incident,
    IncidentRepository,
    InvalidStateTransitionError,
    Recommendation,
)
from radar_telemetry import FeedbackMetrics
from sqlalchemy.ext.asyncio import AsyncSession

from radar_feedback_service.callbacks import (
    CallbackParseError,
    InteractionAction,
    ParsedCallback,
    parse_callback,
)
from radar_feedback_service.cards import RcaCardData, format_rca_card
from radar_feedback_service.delivery import card_data_from_rows

InteractionHandler = Callable[[NotificationInteraction], Awaitable[None]]
"""What the Socket Mode source dispatches to: one translated interaction in, no result
— the outcome is recorded in the database and reflected on the card, not returned."""

MentionHandler = Callable[[BotMention], Awaitable[None]]
"""What the source dispatches an ``@radar`` mention to: parse, query, and reply
in-thread. Like the interaction handler, it returns nothing — the reply is posted."""


class InteractionSource(Protocol):
    """The receive-side connection the app owns in its lifespan: start, then close.

    Structural, so the concrete ``SlackSocketSource`` satisfies it without an import
    here — and a test can inject a fake that opens no socket. Constructing a source
    binds the handlers; :meth:`start` connects and :meth:`close` disconnects.
    """

    async def start(self) -> None: ...

    async def close(self) -> None: ...


InteractionSourceFactory = Callable[
    [InteractionHandler, MentionHandler], InteractionSource
]
"""Builds the source from the interaction and mention handlers. Injected so tests
supply a socketless fake; the default (production) factory builds the Slack Socket Mode
source from Vault tokens."""

log = get_logger("feedback.interactions")


class CallbackOutcome(StrEnum):
    """What handling a callback did — the handler's report to its caller."""

    FEEDBACK_RECORDED = "feedback_recorded"
    RESOLVED = "resolved"
    ALREADY_RESOLVED = "already_resolved"


#: The stored ``feedback.sentiment`` for each rating button. The vocabulary the
#: ``FeedbackEvent`` contract documents (``helpful`` / ``not_helpful``); keyed on the
#: action so the mapping cannot drift from the closed :class:`InteractionAction` set.
_SENTIMENT: dict[InteractionAction, str] = {
    InteractionAction.FEEDBACK_UP: "helpful",
    InteractionAction.FEEDBACK_DOWN: "not_helpful",
}

#: The card footer line each rating leaves. ``<@id>`` is Slack's mention syntax, which
#: the card (mrkdwn) renders as the acting user's name.
_FEEDBACK_ACK: dict[InteractionAction, str] = {
    InteractionAction.FEEDBACK_UP: "👍 Marked helpful by",
    InteractionAction.FEEDBACK_DOWN: "👎 Marked not helpful by",
}


def build_interaction_handler(
    database: Database, notifier: NotificationBackend, metrics: FeedbackMetrics
) -> InteractionHandler:
    """The adapter the wired Socket Mode listener dispatches each button click to.

    Bridges the plugin's vendor-neutral :class:`NotificationInteraction` to the parse →
    handle path, closing over the live database and notifier. It is the caller
    :func:`parse_callback` was written FOR: the parser rejects a malformed click by
    RAISING :class:`CallbackParseError`, and the whole point of that strict parse is
    undone if the caller swallows the raise silently — a listener that no-ops on a parse
    failure makes a bad click look handled and quietly discards the deep-treatment
    validation. So the reject is SURFACED here, loudly, in the log. The envelope is
    already acked by the time this runs (the listener acks first, no redelivery), so the
    log is the right surface: nothing to retry, but the reject is never invisible.

    A missing recommendation (:class:`~radar_common.NotFoundError`) is likewise logged,
    not raised on: the card was delivered, so a missing row is corruption, and there is
    no redelivery to gain by propagating — an operator needs to see it, which the log
    gives them.
    """

    async def handle(interaction: NotificationInteraction) -> None:
        try:
            parsed = parse_callback(interaction)
        except CallbackParseError as exc:
            # SURFACE, never swallow. See the docstring: a silent no-op here undoes the
            # parser. The envelope is already acked, so logging is the surface.
            log.warning(
                "interaction.rejected",
                action_id=interaction.action_id,
                user_id=interaction.user_id,
                reason=str(exc),
            )
            return
        try:
            outcome = await handle_callback(database, notifier, parsed, metrics=metrics)
        except NotFoundError as exc:
            log.error(
                "interaction.recommendation_missing",
                recommendation_id=str(parsed.recommendation_id),
                user_id=parsed.user_id,
                reason=str(exc),
            )
            return
        log.info(
            "interaction.handled",
            action=parsed.action.value,
            outcome=outcome.value,
            recommendation_id=str(parsed.recommendation_id),
            user_id=parsed.user_id,
        )

    return handle


async def handle_callback(
    database: Database,
    notifier: NotificationBackend,
    parsed: ParsedCallback,
    *,
    metrics: FeedbackMetrics,
) -> CallbackOutcome:
    """Apply ``parsed`` — record feedback or resolve the incident — and reflect it.

    Records the database change first (the truth), commits, then re-renders the card
    best-effort. Raises :class:`~radar_common.NotFoundError` if the recommendation the
    callback names — or its incident — is missing: the card was delivered, so a missing
    row is corruption, not a race, and is rejected loudly rather than acted on blindly.

    ``metrics`` carries ``feedback_total``; only the vote path ticks it, and only after
    the row commits (see :func:`_record_feedback`). Resolve does not — the counter is
    scoped by sentiment, and a resolve has none.
    """
    if parsed.action is InteractionAction.RESOLVE:
        return await _resolve(database, notifier, parsed)
    return await _record_feedback(database, notifier, parsed, metrics)


async def _record_feedback(
    database: Database,
    notifier: NotificationBackend,
    parsed: ParsedCallback,
    metrics: FeedbackMetrics,
) -> CallbackOutcome:
    """Write one ``feedback`` row for the rating, then acknowledge it on the card."""
    sentiment = _SENTIMENT[parsed.action]
    async with database.session() as session:
        recommendation = await _load_recommendation(session, parsed.recommendation_id)
        incident = await _load_incident(session, recommendation.incident_id)
        session.add(
            Feedback(
                recommendation_id=recommendation.id,
                incident_id=recommendation.incident_id,
                correlation_id=recommendation.correlation_id,
                sentiment=sentiment,
                slack_user_id=parsed.user_id,
                slack_message_ts=parsed.message_ts,
                # Copied from the rated recommendation, not re-derived: feedback is
                # grouped by the provider/alias that produced the RCA being rated.
                llm_provider=recommendation.llm_provider,
                model_alias=recommendation.model_alias,
            )
        )
        await session.commit()
        # AFTER the commit, never before: the counter must count RECORDED feedback, not
        # attempted. A commit that raised never reaches this line, so a rolled-back
        # write leaves the counter untouched — a dashboard reading radar_feedback_total
        # sees only feedback that actually landed. Same after-the-guarantee ordering as
        # delivery's ts-after-post and the transition-in-the-commit.
        metrics.feedback_total.labels(sentiment).inc()
        log.info(
            "feedback.recorded",
            recommendation_id=str(recommendation.id),
            incident_id=str(recommendation.incident_id),
            sentiment=sentiment,
            slack_user_id=parsed.user_id,
        )
        card = card_data_from_rows(recommendation, incident)

    ack = f"{_FEEDBACK_ACK[parsed.action]} <@{parsed.user_id}>"
    await _reflect(notifier, parsed, card, ack=ack)
    return CallbackOutcome.FEEDBACK_RECORDED


async def _resolve(
    database: Database,
    notifier: NotificationBackend,
    parsed: ParsedCallback,
) -> CallbackOutcome:
    """Resolve the incident behind the recommendation, tolerating the loser race."""
    async with database.session() as session:
        recommendation = await _load_recommendation(session, parsed.recommendation_id)
        try:
            incident = await IncidentRepository(session).transition_status(
                recommendation.incident_id,
                STATUS_RESOLVED,
                actor=parsed.user_id,
                correlation_id=recommendation.correlation_id,
                audit_payload={
                    "recommendation_id": str(recommendation.id),
                    "slack_user_id": parsed.user_id,
                    "channel_id": parsed.channel_id,
                },
            )
        except InvalidStateTransitionError as exc:
            # The loser path: the incident is already resolved (a concurrent click, or
            # stage 1 resolved it first). Benign for the user, but the rejected attempt
            # is forensic — the executor wrote nothing, so record it here. Safe in this
            # transaction: the raise happened before any flush.
            session.add(_invalid_transition_audit(exc, recommendation, parsed))
            incident = await _load_incident(session, recommendation.incident_id)
            await session.commit()
            log.info(
                "incident.resolve_loser",
                incident_id=str(exc.incident_id),
                from_status=exc.from_status,
                attempted_status=exc.attempted_status,
                slack_user_id=parsed.user_id,
            )
            outcome = CallbackOutcome.ALREADY_RESOLVED
        else:
            await session.commit()
            log.info(
                "incident.resolved",
                incident_id=str(incident.id),
                recommendation_id=str(recommendation.id),
                slack_user_id=parsed.user_id,
            )
            outcome = CallbackOutcome.RESOLVED
        card = card_data_from_rows(recommendation, incident)

    ack = _resolve_ack(outcome, parsed.user_id)
    await _reflect(notifier, parsed, card, ack=ack)
    return outcome


def _resolve_ack(outcome: CallbackOutcome, user_id: str) -> str:
    if outcome is CallbackOutcome.RESOLVED:
        return f"✅ Resolved by <@{user_id}>"
    return "✅ Already resolved"


def _invalid_transition_audit(
    exc: InvalidStateTransitionError,
    recommendation: Recommendation,
    parsed: ParsedCallback,
) -> AuditLog:
    """The forensic row for a rejected resolve — the attempt the executor won't file."""
    return AuditLog(
        event_type=INVALID_TRANSITION_AUDIT_EVENT,
        entity_type="incident",
        entity_id=exc.incident_id,
        correlation_id=recommendation.correlation_id,
        actor=parsed.user_id,
        payload={
            "from_status": exc.from_status,
            "attempted_status": exc.attempted_status,
            "recommendation_id": str(recommendation.id),
            "slack_user_id": parsed.user_id,
            "channel_id": parsed.channel_id,
        },
    )


async def _reflect(
    notifier: NotificationBackend,
    parsed: ParsedCallback,
    card: RcaCardData,
    *,
    ack: str,
) -> None:
    """Re-render the card in place with ``ack``, best-effort.

    Runs AFTER the commit, so the database already holds the truth. A failed update is
    logged and swallowed — the footer is a mirror, and losing the mirror must not undo a
    recorded rating or a real resolution. The broad ``except`` keeps the vendor's error
    type behind the plugin (nothing in ``apps/`` imports the Slack SDK) while still
    failing visibly in the log; the class name is logged, never a vendor message.
    """
    text, blocks = format_rca_card(card, ack=ack)
    try:
        await notifier.update(parsed.channel_id, parsed.message_ts, text, blocks=blocks)
    except Exception as exc:
        log.warning(
            "interaction.card_update_failed",
            channel_id=parsed.channel_id,
            message_ts=parsed.message_ts,
            error=type(exc).__name__,
        )


async def _load_recommendation(
    session: AsyncSession, recommendation_id: UUID
) -> Recommendation:
    recommendation = await session.get(Recommendation, recommendation_id)
    if recommendation is None:
        raise NotFoundError(f"recommendation {recommendation_id} does not exist")
    return recommendation


async def _load_incident(session: AsyncSession, incident_id: UUID) -> Incident:
    incident = await session.get(Incident, incident_id)
    if incident is None:
        raise NotFoundError(f"incident {incident_id} does not exist")
    return incident
