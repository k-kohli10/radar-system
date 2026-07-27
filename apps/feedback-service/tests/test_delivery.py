"""RCA delivery guarantees, on ``deliver_rca`` directly (real Postgres).

This is the deep-treatment core of Phase 9. Delivering a card is an external side
effect with no idempotency key, so the design is a deliberate AT-LEAST-ONCE:
prefer a duplicate card (visible, annoying) over a missed one (an incident nobody
hears about). Three guarantees carry the weight, each mutation-proven where it
actually lives:

1. **No double-post under concurrency** — the recommendation row lock, held across
   the Slack post. Two concurrent deliveries produce ONE card; the loser blocks,
   re-reads ``slack_message_ts``, and skips. Mutations: drop the ts pre-check (the
   loser posts again), and drop the FOR UPDATE lock (both post).

2. **At-least-once ordering** — ``slack_message_ts`` is written strictly AFTER a
   successful post. A failed post records nothing, so redelivery retries. The
   mutation that matters most: record before the post (mark-then-post), which
   silently converts this to at-MOST-once and loses cards on exactly the failures
   redelivery exists to handle.

3. **The delivery record** — ts + audit row + processed marker commit together, so
   the audit trail never claims a delivery the guard does not also hold.

The FakeNotifier counts its calls: ``len(notifier.calls)`` is "how many cards were
posted", which is what the no-double-post assertions rest on.
"""

from __future__ import annotations

import asyncio
from uuid import UUID, uuid4

import pytest
from fakes import FakeNotifier
from radar_common import NotFoundError
from radar_database import (
    AuditLog,
    Database,
    Incident,
    InvestigationPlan,
    ProcessedEvent,
    Recommendation,
)
from radar_feedback_service.delivery import (
    DeliveryOutcome,
    NotificationFailedError,
    deliver_rca,
)
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

CHANNEL = "#all-my-tech"
SERVICE_NAME = "feedback-service"


async def _seed(db: Database, *, is_fallback: bool = False) -> tuple[UUID, UUID]:
    """Commit an incident + undelivered recommendation. Returns (incident, rec) ids."""
    incident_id = uuid4()
    plan_id = uuid4()
    rec_id = uuid4()
    async with db.session() as session:
        session.add(
            Incident(
                id=incident_id,
                correlation_id=uuid4(),
                fingerprint="f" * 64,
                service_name="order-service",
                title="order-service OrderFailure",
                severity="high",
                status="open",
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
                correlation_id=uuid4(),
                llm_provider="openai",
                model_alias="extended",
                model_id="gpt-4o",
                root_cause="A bad deploy raised the pool timeout.",
                confidence="high",
                recommended_actions=[{"order": 1, "action": "Roll back."}],
                is_fallback=is_fallback,
            )
        )
        await session.commit()
    return incident_id, rec_id


async def _deliver(
    db: Database,
    notifier: FakeNotifier,
    *,
    incident_id: UUID,
    recommendation_id: UUID,
    event_id: UUID,
) -> DeliveryOutcome:
    """Run one delivery in its own committed transaction, as the handler would."""
    async with db.session() as session:
        outcome = await deliver_rca(
            session,
            notifier,
            recommendation_id=recommendation_id,
            incident_id=incident_id,
            channel=CHANNEL,
            event_id=event_id,
            service_name=SERVICE_NAME,
        )
        await session.commit()
    return outcome


async def _rec(db: Database, rec_id: UUID) -> Recommendation:
    async with db.session() as session:
        rec = await session.get(Recommendation, rec_id)
    assert rec is not None
    return rec


async def _count(db: Database, model: type) -> int:
    async with db.session() as session:
        return int(await session.scalar(select(func.count()).select_from(model)) or 0)


# --- the happy path: post once, record everything --------------------------------


async def test_delivers_posts_card_and_records_ts_audit_marker(db: Database) -> None:
    incident_id, rec_id = await _seed(db)
    notifier = FakeNotifier(ts_values=["1720000000.5000"])
    event_id = uuid4()

    outcome = await _deliver(
        db,
        notifier,
        incident_id=incident_id,
        recommendation_id=rec_id,
        event_id=event_id,
    )

    assert outcome is DeliveryOutcome.DELIVERED
    # One card, to the configured channel, with blocks.
    assert len(notifier.calls) == 1
    assert notifier.calls[0]["channel"] == CHANNEL
    assert notifier.calls[0]["blocks"]

    rec = await _rec(db, rec_id)
    assert rec.slack_message_ts == "1720000000.5000"

    # The delivery trail: one notification.delivered audit row, one processed marker.
    async with db.session() as session:
        audits = (
            (
                await session.execute(
                    select(AuditLog).where(
                        AuditLog.event_type == "notification.delivered"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(audits) == 1
    assert audits[0].payload["slack_message_ts"] == "1720000000.5000"
    assert audits[0].payload["channel"] == CHANNEL
    assert audits[0].entity_id == rec_id
    assert await _count(db, ProcessedEvent) == 1


async def test_missing_recommendation_raises_not_found(db: Database) -> None:
    notifier = FakeNotifier()
    with pytest.raises(NotFoundError):
        await _deliver(
            db,
            notifier,
            incident_id=uuid4(),
            recommendation_id=uuid4(),
            event_id=uuid4(),
        )
    assert notifier.calls == []


# --- guarantee 2: at-least-once ordering (the sharpest guard) ---------------------


async def test_failed_post_records_nothing_and_stays_retryable(db: Database) -> None:
    """A raising post leaves NO ts and NO marker — redelivery retries. AT-LEAST-ONCE.

    This is the most important guard in the commit. ``slack_message_ts`` is written
    strictly AFTER ``send`` returns, so a failed send records nothing and the card is
    retried on redelivery. The mutation it stands against is record-then-post
    (mark-then-post): writing ts (or the marker) BEFORE the post silently converts
    delivery to AT-MOST-once — a failed post looks delivered, redelivery skips, and
    the card is lost on exactly the failure redelivery exists to handle. If a future
    refactor moves the record ahead of the post, this test must go red.
    """
    incident_id, rec_id = await _seed(db)
    notifier = FakeNotifier(fail=True)

    with pytest.raises(NotificationFailedError):
        await _deliver(
            db,
            notifier,
            incident_id=incident_id,
            recommendation_id=rec_id,
            event_id=uuid4(),
        )

    # The post was attempted...
    assert len(notifier.calls) == 1
    # ...but nothing was recorded: ts NULL, no audit, no marker. Fully retryable.
    rec = await _rec(db, rec_id)
    assert rec.slack_message_ts is None
    assert await _count(db, AuditLog) == 0
    assert await _count(db, ProcessedEvent) == 0


# --- guarantee 1: no double-post ------------------------------------------------


async def test_redelivery_after_delivery_does_not_repost(db: Database) -> None:
    """A second delivery whose recommendation already has a ts posts NOTHING.

    The ts pre-check under the lock. MUTATION: remove the
    ``slack_message_ts is not None`` skip in deliver_rca -> this second delivery
    posts a duplicate card and ``len(calls)`` becomes 2.
    """
    incident_id, rec_id = await _seed(db)
    notifier = FakeNotifier()

    first = await _deliver(
        db,
        notifier,
        incident_id=incident_id,
        recommendation_id=rec_id,
        event_id=uuid4(),
    )
    assert first is DeliveryOutcome.DELIVERED
    assert len(notifier.calls) == 1

    # A distinct event id (as a requeued/replayed delivery would carry): the gate
    # does not catch it, so the ts pre-check must.
    second = await _deliver(
        db,
        notifier,
        incident_id=incident_id,
        recommendation_id=rec_id,
        event_id=uuid4(),
    )
    assert second is DeliveryOutcome.ALREADY_DELIVERED
    assert len(notifier.calls) == 1  # still one card — no repost


async def test_concurrent_deliveries_post_one_card(db: Database) -> None:
    """Two deliveries race the same recommendation; exactly ONE card is posted.

    The row lock held across the post serializes them. Driven deterministically,
    not by a start-barrier: ``deliver_rca`` does not commit, so transaction A can run
    it (posting via the fake, taking the lock) and HOLD while B runs against the
    still-uncommitted row — the same technique the incident-transition concurrency
    test uses.

    - With FOR UPDATE (correct): B's ``SELECT ... FOR UPDATE`` blocks on A's lock.
      Only once A commits does B read the ts A set and skip — one card.
    - Without it (mutation): B reads ts NULL (A uncommitted), posts a second card.

    Two mutations turn this red: dropping ``.with_for_update()`` (B doesn't block,
    posts) and dropping the ts pre-check (B blocks, then re-reads and posts anyway).
    """
    incident_id, rec_id = await _seed(db)
    notifier = FakeNotifier(ts_values=["1720000000.1111", "1720000000.2222"])

    b_outcome: dict[str, DeliveryOutcome] = {}

    async def deliver_b() -> None:
        async with db.session() as session:
            b_outcome["result"] = await deliver_rca(
                session,
                notifier,
                recommendation_id=rec_id,
                incident_id=incident_id,
                channel=CHANNEL,
                event_id=uuid4(),
                service_name=SERVICE_NAME,
            )
            await session.commit()

    async with db.session() as session_a:
        # A delivers and HOLDS: deliver_rca posts (call 1) and takes the row lock,
        # but does not commit, so the lock stays held here.
        a_outcome = await deliver_rca(
            session_a,
            notifier,
            recommendation_id=rec_id,
            incident_id=incident_id,
            channel=CHANNEL,
            event_id=uuid4(),
            service_name=SERVICE_NAME,
        )
        assert a_outcome is DeliveryOutcome.DELIVERED

        task_b = asyncio.create_task(deliver_b())
        # Let B reach its FOR UPDATE and block (or, mutated, read-stale-and-post).
        await asyncio.sleep(0.3)
        assert not task_b.done(), "B should be blocked on A's row lock, not finished"

        await session_a.commit()  # release the lock; B unblocks
        await task_b

    # A won; B saw the ts A committed and skipped. Exactly ONE card.
    assert b_outcome["result"] is DeliveryOutcome.ALREADY_DELIVERED
    assert len(notifier.calls) == 1

    rec = await _rec(db, rec_id)
    assert rec.slack_message_ts == "1720000000.1111"  # A's ts, not overwritten
    # One delivery => one audit row.
    async with db.session() as session:
        delivered = await session.scalar(
            select(func.count())
            .select_from(AuditLog)
            .where(AuditLog.event_type == "notification.delivered")
        )
    assert delivered == 1


async def test_fallback_recommendation_delivers_ai_unavailable_card(
    db: Database,
) -> None:
    """A fallback recommendation delivers the AI-Unavailable card variant."""
    incident_id, rec_id = await _seed(db, is_fallback=True)
    notifier = FakeNotifier()

    await _deliver(
        db,
        notifier,
        incident_id=incident_id,
        recommendation_id=rec_id,
        event_id=uuid4(),
    )

    assert len(notifier.calls) == 1
    assert "AI Unavailable" in notifier.calls[0]["text"]


async def test_slack_message_ts_is_unique_across_recommendations(db: Database) -> None:
    """The cross-row integrity backstop: no two recommendations share a Slack message.

    Not the no-double-post guard (that is the lock — two deliveries of ONE
    recommendation post two DIFFERENT timestamps that never collide). This is the
    OTHER direction: one Slack message recorded against two DIFFERENT recommendations
    is a bug, and the UNIQUE index on slack_message_ts refuses it at the schema. A
    direct write of the same ts onto two recommendations must raise IntegrityError —
    proving the constraint has teeth, not that it merely exists in the DDL.
    """
    _, rec_a = await _seed(db)
    _, rec_b = await _seed(db)
    shared_ts = "1720000000.9999"

    async with db.session() as session:
        (await session.get(Recommendation, rec_a)).slack_message_ts = shared_ts  # type: ignore[union-attr]
        await session.commit()

    async with db.session() as session:
        (await session.get(Recommendation, rec_b)).slack_message_ts = shared_ts  # type: ignore[union-attr]
        with pytest.raises(IntegrityError):
            await session.commit()
