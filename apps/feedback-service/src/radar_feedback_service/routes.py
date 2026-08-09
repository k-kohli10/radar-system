"""The ``POST /events`` endpoint: idempotency first, then the delivery.

The outbox worker delivers at *least* once, so the first thing this handler does —
before any interpretation of the event at all — is ask ``processed_events`` whether
this ``event_id`` has already been handled by ``feedback-service``. If it has, the
answer is 200 and nothing else happens.

This gate is the EVENT-level idempotency contract every RADAR handler keeps. It is
NOT what prevents a double RCA card: once a recommendation's card is delivered its
``slack_message_ts`` is set, and :func:`~radar_feedback_service.delivery.deliver_rca`
skips on that under a row lock regardless of this gate. The no-double-post
guarantee lives in the delivery lock, not here. This gate keeps redelivery from
redoing settled work and is the standard contract; the two are complementary, not
redundant claims to the same guarantee.

``recommendation.created`` is delivered by :func:`deliver_rca` inside ONE
transaction the handler commits: the ``slack_message_ts``, the delivery audit row,
and the processed marker land together or not at all. The Slack post happens inside
that transaction, holding the recommendation's row lock across it — see the
delivery module for why that is right here and wrong in the reasoner.

Failure mapping is the outbox contract:

- The post failed (:class:`NotificationFailedError`) -> **503, unmarked**. The card
  was not delivered, the transaction rolled back, so redelivery retries. This is the
  at-least-once choice: a failed post is never recorded as done.
- A missing recommendation or incident (:class:`~radar_common.NotFoundError`) ->
  **422**. The reasoner writes the recommendation in the same transaction as the
  event, so a missing one is corruption, not a race — permanent, dead-letter to a
  human rather than retry a fault that will not clear.

An event type belonging to a DIFFERENT service takes the standard mark-and-drop:
it is not ours, nobody else will deliver it, and an error would have the worker
retry it forever.

Status codes are the documented agent contract: 200 processed (or already seen),
401 bad token, 422 malformed payload — and 401 beats 422 (the shared guard in
``radar_common.auth``).
"""

from __future__ import annotations

from collections.abc import Callable

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import ValidationError
from radar_common import EventsAuth, NotFoundError, get_logger
from radar_contracts import (
    EventEnvelope,
    NotificationBackend,
    RecommendationCreatedPayload,
)
from radar_database import Database, is_already_processed, mark_processed
from radar_telemetry import bind_correlation_id

from radar_feedback_service.config import SERVICE_NAME
from radar_feedback_service.delivery import (
    NotificationFailedError,
    deliver_rca,
)

log = get_logger("feedback.routes")

RECOMMENDATION_CREATED_EVENT = "recommendation.created"
"""The event this service consumes: the reasoner has written an RCA to deliver."""


def create_events_router(
    *,
    get_database: Callable[[], Database | None],
    get_notifier: Callable[[], NotificationBackend | None],
    channel: str,
    events_auth: EventsAuth,
) -> APIRouter:
    """Build the ``POST /events`` surface.

    ``get_database`` and ``get_notifier`` return the live database and Slack
    backend (set during startup) or ``None`` when the service is not ready — the
    handler answers 503 then, rather than touching a dependency that is not there.
    ``channel`` is the deployment's RCA channel, from config (never a constant).
    """
    router = APIRouter()

    @router.post("/events", dependencies=[Depends(events_auth.require())])
    async def handle_event(envelope: EventEnvelope) -> dict[str, str]:
        bind_correlation_id(envelope.correlation_id)

        database = get_database()
        notifier = get_notifier()
        if database is None or notifier is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="feedback-service is not ready",
            )

        async with database.session() as session:
            # THE GATE. First read of the handler, before the event is interpreted
            # at all. Event-level idempotency — not the double-post guard (that is
            # the delivery lock); see the module docstring.
            if await is_already_processed(session, envelope.event_id, SERVICE_NAME):
                log.info(
                    "event.already_processed",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                return {"status": "already_processed"}

            if envelope.event_type != RECOMMENDATION_CREATED_EVENT:
                # Not this service's event. An error would have the worker retry it
                # forever; marked seen and dropped, it is delivered exactly once.
                log.warning(
                    "event.unhandled_type",
                    event_id=str(envelope.event_id),
                    event_type=envelope.event_type,
                )
                await mark_processed(session, envelope.event_id, SERVICE_NAME)
                await session.commit()
                return {"status": "ignored"}

            payload = _parse_payload(envelope)

            try:
                outcome = await deliver_rca(
                    session,
                    notifier,
                    recommendation_id=payload.recommendation_id,
                    incident_id=payload.incident_id,
                    channel=channel,
                    event_id=envelope.event_id,
                    service_name=SERVICE_NAME,
                )
            except NotFoundError as exc:
                # A missing recommendation/incident is corruption, not a race:
                # permanent, so dead-letter it to a human rather than retry forever.
                log.error(
                    "rca.precondition_missing",
                    event_id=str(envelope.event_id),
                    detail=str(exc),
                )
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
                ) from exc
            except NotificationFailedError as exc:
                # The post failed. Nothing was recorded (the transaction rolls back
                # on the way out), so redelivery retries — at-least-once. Retryable
                # 503, deliberately NOT marked processed.
                log.warning(
                    "rca.delivery_failed",
                    event_id=str(envelope.event_id),
                    reason=str(exc),
                )
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=str(exc)
                ) from exc

            await session.commit()

        return {"status": outcome.value}

    return router


def _parse_payload(envelope: EventEnvelope) -> RecommendationCreatedPayload:
    """Parse the event body, or 422 on a malformed one.

    A widened or wrong-shaped payload is a permanent fault — the reasoner and this
    service disagree on the contract — so it dead-letters rather than retrying.
    """
    try:
        return RecommendationCreatedPayload.model_validate(envelope.payload)
    except ValidationError as exc:
        log.warning(
            "event.malformed_payload",
            event_id=str(envelope.event_id),
            event_type=envelope.event_type,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        ) from exc
