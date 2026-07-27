"""The interaction handler: a parsed card click becomes feedback or a resolution.

The handler is what :func:`parse_callback` feeds. These tests run against real
Postgres because every guarantee here is a database one — a ``feedback`` row with
its foreign keys, a validated status transition under a row lock, and the forensic
audit the resolve loser leaves. A mock cannot prove any of them.

The load-bearing case is the **resolve loser**. Two engineers clicking Resolve on the
same card is not exotic; it is the common case. ``transition_status`` serializes them
on the incident's row lock — the winner moves ``open -> resolved``, the loser re-reads
``resolved`` and is rejected. The rejection must be benign to the user AND leave a
forensic ``incident.invalid_transition`` row (the executor writes nothing; recording
the attempt is the caller's job, ADR 0016). ``test_concurrent_resolves_...`` proves
both under a real race, not a simulated one.
"""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest
import structlog
from fakes import FakeNotifier
from prometheus_client import CollectorRegistry
from radar_common import NotFoundError
from radar_database import (
    AuditLog,
    Database,
    Feedback,
    Incident,
    InvestigationPlan,
    Recommendation,
)
from radar_feedback_service.callbacks import InteractionAction, ParsedCallback
from radar_feedback_service.interactions import (
    CallbackOutcome,
    build_interaction_handler,
    handle_callback,
)
from radar_plugin_notifications_slack import ack_and_dispatch
from radar_telemetry import IncidentMetrics, create_incident_metrics
from slack_sdk.socket_mode.request import SocketModeRequest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

CHANNEL_ID = "C0FEEDBACK"
USER_ID = "U0ENGINEER"
MESSAGE_TS = "1720000000.0001"


def _metrics() -> IncidentMetrics:
    """A throwaway incident-metrics family on its own registry — for the tests that
    exercise handling but do not assert on the counter. The two metric tests build their
    own so they can read radar_feedback_total back off the registry."""
    return create_incident_metrics(CollectorRegistry())


async def _seed(
    db: Database, *, incident_status: str = "open"
) -> tuple[UUID, UUID, UUID]:
    """Seed an incident, plan, and its recommendation; return their ids + the
    recommendation's correlation_id (distinct from the incident's, so a test can tell
    the feedback row inherited the RECOMMENDATION's, not the incident's)."""
    incident_id = uuid4()
    plan_id = uuid4()
    rec_id = uuid4()
    rec_correlation = uuid4()
    async with db.session() as session:
        session.add(
            Incident(
                id=incident_id,
                correlation_id=uuid4(),
                fingerprint="f" * 64,
                service_name="order-service",
                title="order-service OrderFailure",
                severity="high",
                status=incident_status,
            )
        )
        session.add(
            InvestigationPlan(
                id=plan_id,
                incident_id=incident_id,
                correlation_id=uuid4(),
                steps=[{"order": 1, "description": "check deploys"}],
            )
        )
        session.add(
            Recommendation(
                id=rec_id,
                incident_id=incident_id,
                plan_id=plan_id,
                correlation_id=rec_correlation,
                llm_provider="openai",
                model_alias="extended",
                model_id="gpt-4o",
                root_cause="A bad deploy raised the pool timeout.",
                confidence="high",
                recommended_actions=[{"order": 1, "action": "Roll back."}],
            )
        )
        await session.commit()
    return incident_id, rec_id, rec_correlation


def _callback(action: InteractionAction, rec_id: UUID) -> ParsedCallback:
    return ParsedCallback(
        action=action,
        recommendation_id=rec_id,
        user_id=USER_ID,
        channel_id=CHANNEL_ID,
        message_ts=MESSAGE_TS,
    )


async def _feedback_rows(db: Database, rec_id: UUID) -> list[Feedback]:
    async with db.session() as session:
        rows = (
            await session.execute(
                select(Feedback).where(Feedback.recommendation_id == rec_id)
            )
        ).scalars()
        return list(rows)


async def _status(db: Database, incident_id: UUID) -> str:
    async with db.session() as session:
        incident = await session.get(Incident, incident_id)
    assert incident is not None
    return incident.status


async def _count_audits(db: Database, incident_id: UUID, event_type: str) -> int:
    async with db.session() as session:
        count = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(
                AuditLog.entity_id == incident_id,
                AuditLog.event_type == event_type,
            )
        )
    return int(count or 0)


def _context_texts(update: dict[str, object]) -> list[str]:
    blocks = update["blocks"]
    assert isinstance(blocks, list)
    texts: list[str] = []
    for block in blocks:
        if block.get("type") == "context":
            for element in block["elements"]:
                texts.append(element["text"])
    return texts


async def test_thumbs_up_records_helpful_feedback(db: Database) -> None:
    """👍 writes one feedback row, sentiment `helpful`, provider/alias/correlation
    copied from the RECOMMENDATION being rated, stamped with the acting user."""
    incident_id, rec_id, rec_correlation = await _seed(db)
    notifier = FakeNotifier()

    outcome = await handle_callback(
        db,
        notifier,
        _callback(InteractionAction.FEEDBACK_UP, rec_id),
        metrics=_metrics(),
    )

    assert outcome is CallbackOutcome.FEEDBACK_RECORDED
    rows = await _feedback_rows(db, rec_id)
    assert len(rows) == 1
    row = rows[0]
    assert row.sentiment == "helpful"
    assert row.incident_id == incident_id
    assert row.correlation_id == rec_correlation
    assert row.llm_provider == "openai"
    assert row.model_alias == "extended"
    assert row.slack_user_id == USER_ID
    assert row.slack_message_ts == MESSAGE_TS


async def test_thumbs_down_records_not_helpful_feedback(db: Database) -> None:
    """👎 is the same write with sentiment `not_helpful` — the closed vocabulary."""
    _, rec_id, _ = await _seed(db)

    outcome = await handle_callback(
        db,
        FakeNotifier(),
        _callback(InteractionAction.FEEDBACK_DOWN, rec_id),
        metrics=_metrics(),
    )

    assert outcome is CallbackOutcome.FEEDBACK_RECORDED
    rows = await _feedback_rows(db, rec_id)
    assert len(rows) == 1
    assert rows[0].sentiment == "not_helpful"


async def test_feedback_reflects_on_the_card(db: Database) -> None:
    """The recorded rating is mirrored back onto the card: one chat.update carrying an
    acknowledgement footer. The mirror is what an engineer scrolling back sees."""
    _, rec_id, _ = await _seed(db)
    notifier = FakeNotifier()

    cb = _callback(InteractionAction.FEEDBACK_UP, rec_id)
    await handle_callback(db, notifier, cb, metrics=_metrics())

    assert len(notifier.updates) == 1
    update = notifier.updates[0]
    assert update["channel"] == CHANNEL_ID
    assert update["message_ref"] == MESSAGE_TS
    assert any("Marked helpful" in text for text in _context_texts(update))


async def test_resolve_transitions_incident_and_audits(db: Database) -> None:
    """Resolve moves the incident behind the recommendation to `resolved` through the
    validated entry point, writing exactly one incident.resolved audit row."""
    incident_id, rec_id, _ = await _seed(db, incident_status="open")
    notifier = FakeNotifier()

    outcome = await handle_callback(
        db, notifier, _callback(InteractionAction.RESOLVE, rec_id), metrics=_metrics()
    )

    assert outcome is CallbackOutcome.RESOLVED
    assert await _status(db, incident_id) == "resolved"
    assert await _count_audits(db, incident_id, "incident.resolved") == 1
    # The card is reflected into its resolved state.
    assert len(notifier.updates) == 1
    assert any("Resolved by" in text for text in _context_texts(notifier.updates[0]))


async def test_resolve_of_already_resolved_incident_is_benign_and_audited(
    db: Database,
) -> None:
    """The loser path in isolation: resolving an already-resolved incident does not
    raise, returns ALREADY_RESOLVED, writes NO second incident.resolved, and leaves one
    forensic incident.invalid_transition row carrying both ends of the rejected edge."""
    incident_id, rec_id, _ = await _seed(db, incident_status="resolved")

    outcome = await handle_callback(
        db,
        FakeNotifier(),
        _callback(InteractionAction.RESOLVE, rec_id),
        metrics=_metrics(),
    )

    assert outcome is CallbackOutcome.ALREADY_RESOLVED
    assert await _status(db, incident_id) == "resolved"
    assert await _count_audits(db, incident_id, "incident.resolved") == 0
    assert await _count_audits(db, incident_id, "incident.invalid_transition") == 1
    async with db.session() as session:
        audit = (
            await session.execute(
                select(AuditLog).where(
                    AuditLog.entity_id == incident_id,
                    AuditLog.event_type == "incident.invalid_transition",
                )
            )
        ).scalar_one()
    assert audit.payload["from_status"] == "resolved"
    assert audit.payload["attempted_status"] == "resolved"
    assert audit.payload["recommendation_id"] == str(rec_id)


async def test_concurrent_resolves_one_wins_one_loses(db: Database) -> None:
    """Two engineers click Resolve at once. The incident's row lock serializes them:
    exactly one RESOLVED and one ALREADY_RESOLVED, the incident resolved exactly once
    (one incident.resolved), and the loser's attempt recorded exactly once
    (one incident.invalid_transition).

    This is the loser path under a REAL race, and the teeth of the guarantee: the
    forensic count is exactly one because the loser is caught and audited, not lost
    and not double-counted. Drop the loser's audit write and the invalid_transition
    assertion turns red; drop the FOR UPDATE serialization and both could resolve.
    """
    incident_id, rec_id, _ = await _seed(db, incident_status="open")

    cb = _callback(InteractionAction.RESOLVE, rec_id)
    outcomes = await asyncio.gather(
        handle_callback(db, FakeNotifier(), cb, metrics=_metrics()),
        handle_callback(db, FakeNotifier(), cb, metrics=_metrics()),
    )

    assert set(outcomes) == {
        CallbackOutcome.RESOLVED,
        CallbackOutcome.ALREADY_RESOLVED,
    }
    assert await _status(db, incident_id) == "resolved"
    assert await _count_audits(db, incident_id, "incident.resolved") == 1
    assert await _count_audits(db, incident_id, "incident.invalid_transition") == 1


async def test_card_update_failure_does_not_undo_feedback(db: Database) -> None:
    """Card reflection is best-effort AFTER the commit: a failing chat.update is
    swallowed and the feedback row still stands. The database is the truth; losing the
    mirror must never roll back a recorded rating.

    MUTATION guard: move the update ahead of the commit (or let it propagate) and a
    failing Slack call would leave zero feedback rows, turning this red."""
    _, rec_id, _ = await _seed(db)

    outcome = await handle_callback(
        db,
        FakeNotifier(fail=True),
        _callback(InteractionAction.FEEDBACK_UP, rec_id),
        metrics=_metrics(),
    )

    assert outcome is CallbackOutcome.FEEDBACK_RECORDED
    assert len(await _feedback_rows(db, rec_id)) == 1


async def test_missing_recommendation_is_rejected(db: Database) -> None:
    """A callback naming a recommendation that does not exist is corruption (the card
    was delivered), not a race — rejected loudly, never acted on blindly."""
    with pytest.raises(NotFoundError):
        await handle_callback(
            db,
            FakeNotifier(),
            _callback(InteractionAction.FEEDBACK_UP, uuid4()),
            metrics=_metrics(),
        )


# --- radar_feedback_total: counts RECORDED feedback, after the commit -----------------


def _feedback_count(registry: CollectorRegistry, sentiment: str) -> float | None:
    """radar_feedback_total for one sentiment, read off the registry — None if the
    counter never ticked for that label (a never-incremented counter has no sample)."""
    return registry.get_sample_value("radar_feedback_total", {"sentiment": sentiment})


async def test_feedback_increments_counter_by_sentiment(db: Database) -> None:
    """A recorded 👍 ticks radar_feedback_total{sentiment=helpful} exactly once, and 👎
    the not_helpful series — the label a dashboard splits votes on."""
    _, up_rec, _ = await _seed(db)
    _, down_rec, _ = await _seed(db)
    registry = CollectorRegistry()
    metrics = create_incident_metrics(registry)

    await handle_callback(
        db,
        FakeNotifier(),
        _callback(InteractionAction.FEEDBACK_UP, up_rec),
        metrics=metrics,
    )
    await handle_callback(
        db,
        FakeNotifier(),
        _callback(InteractionAction.FEEDBACK_DOWN, down_rec),
        metrics=metrics,
    )

    assert _feedback_count(registry, "helpful") == 1.0
    assert _feedback_count(registry, "not_helpful") == 1.0


async def test_resolve_does_not_touch_feedback_counter(db: Database) -> None:
    """Resolve is not a sentiment-bearing feedback submission, so it ticks no series of
    radar_feedback_total. (Resolve is observable via the incident.resolved audit.)"""
    _, rec_id, _ = await _seed(db, incident_status="open")
    registry = CollectorRegistry()
    metrics = create_incident_metrics(registry)

    await handle_callback(
        db,
        FakeNotifier(),
        _callback(InteractionAction.RESOLVE, rec_id),
        metrics=metrics,
    )

    assert _feedback_count(registry, "helpful") is None
    assert _feedback_count(registry, "not_helpful") is None


async def test_counter_not_incremented_when_the_write_rolls_back(db: Database) -> None:
    """The load-bearing half: a 👍 whose DB commit FAILS must NOT tick the counter.

    radar_feedback_total counts feedback that was RECORDED; a dashboard reads it as
    'feedback received'. So a rolled-back write that left no row must leave no count —
    otherwise the metric claims feedback the database never kept.

    The commit is forced to raise; the increment sits AFTER it, so it never runs and the
    counter stays empty (and no row lands). MUTATION guard: move the .inc() ahead of the
    commit and this turns red — the counter would read 1.0 for a write that rolled back.
    This is the assertion that actually pins the after-commit ordering; the success test
    passes whether the increment is before or after.
    """
    _, rec_id, _ = await _seed(db)
    registry = CollectorRegistry()
    metrics = create_incident_metrics(registry)

    with patch.object(
        AsyncSession, "commit", AsyncMock(side_effect=RuntimeError("db write failed"))
    ):
        with pytest.raises(RuntimeError):
            await handle_callback(
                db,
                FakeNotifier(),
                _callback(InteractionAction.FEEDBACK_UP, rec_id),
                metrics=metrics,
            )

    assert _feedback_count(registry, "helpful") is None
    assert await _feedback_rows(db, rec_id) == []


# --- driven through the WIRED listener (ack_and_dispatch + the real adapter) ---------
#
# These do not call handle_callback directly: they push a Slack block_actions payload
# through the plugin's real ack/translate/dispatch shell into the adapter the app wires
# onto the socket. A fake client stands in for the WebSocket; everything else is the
# production receive path.


def _block_actions(
    *, action_id: str, value: str | None, ts: str = MESSAGE_TS
) -> dict[str, Any]:
    """A Slack block_actions payload, as Socket Mode delivers on a button click."""
    action: dict[str, Any] = {"action_id": action_id, "block_id": "b1"}
    if value is not None:
        action["value"] = value
    return {
        "type": "block_actions",
        "actions": [action],
        "user": {"id": USER_ID},
        "channel": {"id": CHANNEL_ID},
        "message": {"ts": ts},
    }


async def _drive(db: Database, notifier: FakeNotifier, payload: dict[str, Any]) -> None:
    """Push ``payload`` through the real listener shell into the wired adapter."""
    handler = build_interaction_handler(db, notifier, _metrics())
    request = SocketModeRequest(
        type="interactive", envelope_id="env-1", payload=payload
    )
    await ack_and_dispatch(AsyncMock(), request, handler)


async def test_good_click_through_the_listener_records_feedback(db: Database) -> None:
    """The wired path end-to-end: a well-formed 👍 click delivered over the listener
    parses, dispatches, and lands one feedback row — the socket-to-database round trip
    minus the socket."""
    _, rec_id, _ = await _seed(db)

    await _drive(
        db,
        FakeNotifier(),
        _block_actions(action_id="feedback.up", value=str(rec_id)),
    )

    rows = await _feedback_rows(db, rec_id)
    assert len(rows) == 1
    assert rows[0].sentiment == "helpful"


async def test_bad_payload_through_the_listener_surfaces_the_reject(
    db: Database,
) -> None:
    """The load-bearing guarantee of the wire-up: an unparseable click driven through
    the listener SURFACES the CallbackParseError reject (a logged `interaction.rejected`
    event) rather than being swallowed.

    A listener that no-ops on a parse failure would silently undo the whole
    deep-treatment parser — a malformed click would look handled while nothing happened,
    and no operator would ever see it. So the adapter logs the reject loudly. The
    envelope is still acked (the plugin acks before dispatch), so the click is not
    redelivered — the log is the surface, and it must exist.

    MUTATION guard: replace the adapter's `log.warning(...)` on CallbackParseError with
    a bare return and this assertion turns red — the silent-swallow this test forbids.
    Two unparseable shapes are driven (unknown action, non-UUID value) so the surface
    holds for both branches the parser rejects on.
    """
    for payload in (
        _block_actions(action_id="bogus.action", value=str(uuid4())),
        _block_actions(action_id="feedback.up", value="not-a-uuid"),
    ):
        with structlog.testing.capture_logs() as captured:
            await _drive(db, FakeNotifier(), payload)
        rejected = [e for e in captured if e["event"] == "interaction.rejected"]
        assert rejected, f"reject was swallowed for {payload['actions'][0]}"
        # Nothing was written — the malformed click did not become feedback.
        assert await _feedback_rows(db, uuid4()) == []
