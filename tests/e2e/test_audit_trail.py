"""The audit_log is populated for every key pipeline event — with teeth.

Phase 13's security deliverable: not "the table is writable" but "every stage of
the pipeline left its own audit row." One alert driven end to end must produce one
audit row per stage — ingestion opening the incident, the watcher requesting a
plan, the planner creating it, the reasoner recording the recommendation,
feedback-service delivering the card, and the lifecycle transition that delivery
drives — each attributed to the service that performed it and linked to the entity
it acted on.

The assertion is EXACT (the full set, not a subset): a stage that stops auditing
reddens it because its row goes missing, and a stage added later reddens it because
the set no longer matches — the same discipline as the correlation test's
``len(...) == 4``. Teeth are proven by mutation: drop any stage's audit write (e.g.
the ``session.add(_incident_opened_audit(...))`` in ingestion) and the expected row
disappears from the set, turning this red.
"""

from __future__ import annotations

from radar_database import AuditLog, Incident, Recommendation
from sqlalchemy import select

from tests.e2e.harness import Pipeline

#: event_type -> (entity_type, actor) for every audit row the happy path writes,
#: one per pipeline stage. Spelled out as literals rather than imported from each
#: service so a rename in a service is caught HERE — these are the durable names a
#: security auditor greps for, and importing the constants would make the test
#: agree with any rename automatically.
EXPECTED_AUDIT_ROWS: dict[str, tuple[str, str]] = {
    "ingestion.incident_opened": ("incident", "ingestion"),
    "watcher.plan_requested": ("incident", "watcher-agent"),
    "planner.plan_created": ("incident", "planner-agent"),
    "reasoner.recommendation_created": ("incident", "reasoner-agent"),
    "notification.delivered": ("recommendation", "feedback-service"),
    "incident.investigating": ("incident", "feedback-service"),
}


async def test_every_pipeline_stage_writes_its_audit_row(pipeline: Pipeline) -> None:
    """One alert → one audit row per stage, each with the right actor and entity."""
    await pipeline.post_alert()
    await pipeline.drain()

    async with pipeline.db.session() as session:
        incident = (await session.scalars(select(Incident))).one()
        rec = (await session.scalars(select(Recommendation))).one()
        rows = list(
            await session.execute(
                select(
                    AuditLog.event_type,
                    AuditLog.entity_type,
                    AuditLog.entity_id,
                    AuditLog.actor,
                )
            )
        )

    # Exactly one row per stage — no stage skipped, none doubled, none extra.
    by_event = {r.event_type: r for r in rows}
    assert len(by_event) == len(rows), (
        f"a stage wrote more than one audit row: {[r.event_type for r in rows]}"
    )
    assert set(by_event) == set(EXPECTED_AUDIT_ROWS), (
        "audit coverage changed: "
        f"expected {sorted(EXPECTED_AUDIT_ROWS)}, got {sorted(by_event)}"
    )

    # Each row is attributed to the service that performed the action and linked to
    # the entity it acted on — a trail an auditor can actually follow, not just a
    # count of rows.
    entity_id_for = {"incident": incident.id, "recommendation": rec.id}
    for event_type, (entity_type, actor) in EXPECTED_AUDIT_ROWS.items():
        row = by_event[event_type]
        assert row.entity_type == entity_type, f"{event_type}: wrong entity_type"
        assert row.actor == actor, f"{event_type}: wrong actor"
        assert row.entity_id == entity_id_for[entity_type], (
            f"{event_type}: audit row points at the wrong {entity_type}"
        )
