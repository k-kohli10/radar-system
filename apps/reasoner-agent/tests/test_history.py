"""Lever 3: the historical-cause prior and the feedback loop, with teeth.

The claim is not "a field exists on the bundle" — it is "prior recommendations and
the feedback on them, sitting in Postgres, are summarized into the bundle the model
reasons from." So these tests SEED a real history — prior incidents for the same
fingerprint, with known root causes and known 👍/👎 — and assert the summary the
reasoner builds matches it, reaches the rendered prompt, and that the system prompt
tells the model to weight it (the mechanism by which it shifts confidence).

Mutation check: break the fingerprint match, the fallback exclusion, or the classifier
and the asserted base rate stops matching the seeded one — these go red, because they
assert the CONTENT of the summary, not merely its presence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from radar_database import (
    Alert,
    Database,
    Feedback,
    Incident,
    InvestigationPlan,
    Recommendation,
)
from radar_reasoner_agent.context import build_context_bundle
from radar_reasoner_agent.history import (
    OTHER_CATEGORY,
    classify_cause,
)
from radar_reasoner_agent.llm import SYSTEM_PROMPT, render_user_message

SERVICE = "order-service"
ALERT = "OrderProcessingFailureRate"
FP = "a" * 64
OTHER_FP = "b" * 64
T0 = datetime(2026, 7, 14, 10, 0, 0, tzinfo=UTC)
_clock = iter(range(10_000))


def _next_time() -> datetime:
    return T0 + timedelta(seconds=next(_clock))


async def _seed_occurrence(
    db: Database,
    *,
    fingerprint: str = FP,
    root_cause: str,
    is_fallback: bool = False,
    sentiment: str | None = None,
    correction_text: str | None = None,
) -> UUID:
    """Seed one past incident for ``fingerprint`` with its recommendation (+feedback).

    Returns the incident id. No alert rows: the history summary reads only
    incidents, recommendations, and feedback.
    """
    incident = Incident(
        id=uuid4(),
        correlation_id=uuid4(),
        fingerprint=fingerprint,
        service_name=SERVICE,
        title=f"{SERVICE} {ALERT}",
        severity="high",
        status="resolved",
        alert_count=1,
        opened_at=_next_time(),
        updated_at=_next_time(),
    )
    plan = InvestigationPlan(
        id=uuid4(),
        incident_id=incident.id,
        correlation_id=incident.correlation_id,
        steps=[{"order": 1, "description": "step"}],
    )
    recommendation = Recommendation(
        id=uuid4(),
        incident_id=incident.id,
        plan_id=plan.id,
        correlation_id=incident.correlation_id,
        llm_provider="none" if is_fallback else "openai",
        model_alias="none" if is_fallback else "extended",
        model_id="template-fallback" if is_fallback else "gpt-4o",
        root_cause=root_cause,
        confidence="low" if is_fallback else "high",
        recommended_actions=[{"order": 1, "action": "do the thing"}],
        is_fallback=is_fallback,
        created_at=_next_time(),
    )
    async with db.session() as session:
        session.add_all([incident, plan, recommendation])
        if sentiment is not None or correction_text is not None:
            session.add(
                Feedback(
                    id=uuid4(),
                    recommendation_id=recommendation.id,
                    incident_id=incident.id,
                    correlation_id=incident.correlation_id,
                    sentiment=sentiment or "correction",
                    correction_text=correction_text,
                    llm_provider="openai",
                    model_alias="extended",
                    created_at=_next_time(),
                )
            )
        await session.commit()
    return incident.id


async def _seed_target(db: Database, *, fingerprint: str = FP) -> tuple[UUID, UUID]:
    """The incident being reasoned about now: incident + alert + plan, no RCA yet."""
    incident = Incident(
        id=uuid4(),
        correlation_id=uuid4(),
        fingerprint=fingerprint,
        service_name=SERVICE,
        title=f"{SERVICE} {ALERT}",
        severity="critical",
        status="open",
        alert_count=1,
        opened_at=_next_time(),
        updated_at=_next_time(),
    )
    plan = InvestigationPlan(
        id=uuid4(),
        incident_id=incident.id,
        correlation_id=incident.correlation_id,
        steps=[{"order": 1, "description": "Check recent deployments"}],
    )
    alert = Alert(
        id=uuid4(),
        source="mock",
        fingerprint=fingerprint,
        service_name=SERVICE,
        alert_name=ALERT,
        severity="critical",
        status="firing",
        raw_payload={},
        labels={},
        annotations={},
        fired_at=T0,
        received_at=T0,
        incident_id=incident.id,
        correlation_id=incident.correlation_id,
    )
    async with db.session() as session:
        session.add_all([incident, plan, alert])
        await session.commit()
    return incident.id, plan.id


# ----------------------------------------------------------- classifier (unit)


@pytest.mark.parametrize(
    ("root_cause", "expected"),
    [
        ("Recent deployment v2.4.1 introduced a bad validation handler", "deployment"),
        ("The upstream payment-gateway dependency was returning 503s", "dependency"),
        ("order-db connection pool was saturated, queries deadlocked", "database"),
        ("order-service ran out of memory (OOM) after a slow leak", "resource"),
        ("A feature flag was misconfigured for the checkout path", "configuration"),
        ("DNS resolution for the inventory service was failing", "network"),
        ("A traffic surge overloaded the service past its rate limit", "traffic"),
        ("Something inexplicable and unprecedented happened", OTHER_CATEGORY),
    ],
)
def test_classify_cause_buckets_by_keyword(root_cause: str, expected: str) -> None:
    assert classify_cause(root_cause) == expected


# ------------------------------------------------------ the prior (real Postgres)


async def test_prior_summarizes_past_causes_into_base_rates(db: Database) -> None:
    """Seed a known cause history; the bundle's prior must report those base rates."""
    await _seed_occurrence(db, root_cause="Bad deploy v1 broke the handler")
    await _seed_occurrence(db, root_cause="Rollout v2 regressed order validation")
    await _seed_occurrence(db, root_cause="A deploy pushed a broken config path")
    await _seed_occurrence(db, root_cause="Upstream payment-gateway dependency down")
    # A fallback RCA for the SAME fingerprint must NOT be counted — "AI unavailable"
    # is the absence of a cause.
    await _seed_occurrence(
        db,
        root_cause="AI analysis unavailable. Manual investigation.",
        is_fallback=True,
    )
    # And a different alert's history must not bleed in.
    await _seed_occurrence(
        db, fingerprint=OTHER_FP, root_cause="Some unrelated deploy elsewhere"
    )

    incident_id, plan_id = await _seed_target(db)
    async with db.session() as session:
        bundle = await build_context_bundle(
            session, incident_id=incident_id, plan_id=plan_id
        )

    prior = bundle.historical_prior
    # three deploys + one dependency; the fallback and the other fingerprint are out.
    assert prior.total == 4
    assert prior.category_counts == {"deployment": 3, "dependency": 1}


async def test_feedback_loop_surfaces_confirmed_and_downweights_rejected(
    db: Database,
) -> None:
    """👍 causes are confirmed, 👎 counted, 📝 corrections surfaced — for this alert."""
    await _seed_occurrence(
        db,
        root_cause="Deploy v3 introduced the failing validation handler",
        sentiment="helpful",
    )
    await _seed_occurrence(
        db,
        root_cause="Guessed it was a memory leak (it was not)",
        sentiment="not_helpful",
    )
    await _seed_occurrence(
        db,
        root_cause="Suspected the database",
        correction_text="It was actually a bad canary deploy, rolled back at 10:42.",
    )

    incident_id, plan_id = await _seed_target(db)
    async with db.session() as session:
        bundle = await build_context_bundle(
            session, incident_id=incident_id, plan_id=plan_id
        )

    fb = bundle.past_feedback
    assert fb.confirmed_causes == [
        "Deploy v3 introduced the failing validation handler"
    ]
    assert fb.unhelpful_count == 1
    assert fb.corrections == [
        "It was actually a bad canary deploy, rolled back at 10:42."
    ]


async def test_the_prior_and_feedback_reach_the_rendered_prompt(db: Database) -> None:
    """ "Reaches the bundle" means reaches the MODEL: the summary is in the prompt."""
    await _seed_occurrence(db, root_cause="Bad deploy broke it")
    await _seed_occurrence(
        db,
        root_cause="Confirmed: upstream dependency outage",
        sentiment="helpful",
    )

    incident_id, plan_id = await _seed_target(db)
    async with db.session() as session:
        bundle = await build_context_bundle(
            session, incident_id=incident_id, plan_id=plan_id
        )

    rendered = render_user_message(bundle)
    assert "historical_prior" in rendered
    assert '"deployment": 1' in rendered
    assert "Confirmed: upstream dependency outage" in rendered


def test_system_prompt_instructs_the_model_to_weight_history() -> None:
    """The confidence-shift mechanism: the prompt must tell the model to use history.

    Without this, the bundle would carry the prior but the model would have no
    instruction to weight it — the signal would reach the prompt and change nothing.
    """
    assert "historical_prior" in SYSTEM_PROMPT
    assert "past_feedback" in SYSTEM_PROMPT
    assert "base rate" in SYSTEM_PROMPT
    assert "confirmed" in SYSTEM_PROMPT.lower()
    # It must also tell the model what to do when there is NO history.
    assert "no history" in SYSTEM_PROMPT.lower()


async def test_novel_fingerprint_has_empty_history(db: Database) -> None:
    """First time an alert fires: total 0 and no confirmed causes."""
    incident_id, plan_id = await _seed_target(db)
    async with db.session() as session:
        bundle = await build_context_bundle(
            session, incident_id=incident_id, plan_id=plan_id
        )

    assert bundle.historical_prior.total == 0
    assert bundle.historical_prior.category_counts == {}
    assert bundle.past_feedback.confirmed_causes == []
    assert bundle.past_feedback.unhelpful_count == 0
