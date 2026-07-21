"""Phase 8's done-condition: an RCA grounded in the runbook, from the ALERT.

Not a context-API unit test — the whole pipeline: a mock alert enters ingestion,
the watcher and planner do their real work, the outbox worker cranks every hop,
and the reasoner fetches graded context from the REAL knowledge service (real
Elasticsearch, real embeddings, real CRAG grading through the real llm-gateway)
before calling the real model. The proof is read where the plan says to read it:
the recommendation row in Postgres.

BOTH DIRECTIONS, BECAUSE CRAG EXISTS
-------------------------------------
- A COVERED alert must yield a grounded RCA whose text references content that
  exists only in the runbook. Generic SRE advice would be an ambiguous pass, so
  grounding is detected by DISTINCTIVE tokens (the 1.5GB threshold, the OOM
  mechanics, the ecommerce namespace) — the same technique the prompt probe
  used: content the model could not produce from general knowledge.
- A NO-COVERAGE alert (right service, a failure class no runbook covers) must
  yield an honestly ungrounded RCA: retrieval ran, CRAG judged nothing
  relevant, the stored wrapper says ``empty`` — not ``unavailable`` — and the
  model was shown an empty slot rather than the least-bad wrong runbook.

The model's words are its own, so the covered assertion is a generous
disjunction over distinctive runbook tokens rather than a phrase match; the
SYSTEM's behaviour (what was retrieved, what was graded, what was stored) is
asserted exactly.

Requires OPENAI_API_KEY, Postgres, Elasticsearch with the built index::

    pytest tests/e2e/test_knowledge_assisted_rca.py -m "live and infra"
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from radar_database import Recommendation
from sqlalchemy import select

from tests.e2e.harness import Pipeline

pytestmark = [pytest.mark.live, pytest.mark.infra]

#: Tier-1: the alert can really fire, and order-service-high-memory documents it.
COVERED_ALERT = {
    "service_name": "order-service",
    "alert_name": "OrderServiceHighMemory",
    "severity": "medium",
}

#: Content that exists in the runbook and nowhere in the model's generic
#: knowledge of "high memory": its exact thresholds, platform specifics, and
#: the leak-vs-plateau trajectory language its Likely Causes section uses. The
#: RCA referencing ANY of these is evidence of grounding; referencing none
#: while the bundle carried the chunks would mean the model ignored them.
#:
#: The list is deliberately WIDE. The first live runs surfaced why: one draw
#: grounded its root cause in the runbook's "steady climb that survives traffic
#: troughs" distinction — verbatim runbook reasoning — while containing none of
#: an earlier six-token list. The model grounds reliably but phrases variably,
#: so the detector must cover the runbook's distinctive phrases broadly; every
#: entry here is still runbook-sourced, so a hit remains evidence of grounding
#: rather than of generic SRE vocabulary.
RUNBOOK_TOKENS = (
    "1.5",
    "2gb",
    "2 gb",
    "oom",
    "plateau",
    "ecommerce",
    "trough",
    "steady climb",
    "levelling off",
    "leveling off",
    "climbs steadily",
)

#: Right service, wrong failure class: no runbook covers a data-handling
#: incident, so CRAG must return nothing and the RCA must say so. The fixture
#: gives this alert a SYMPTOM-RICH plan template, keeping the query off the
#: measured decision boundary — see the fixture and probes.yaml for the
#: property this deliberately avoids asserting on.
NO_COVERAGE_ALERT = {
    "service_name": "inventory-service",
    "alert_name": "InventoryCustomerPiiExposedInLogs",
    "severity": "high",
}


async def _one_recommendation(pipeline: Pipeline) -> Recommendation:
    async with pipeline.db.session() as session:
        rows = list(await session.scalars(select(Recommendation)))
    assert len(rows) == 1, f"expected exactly one recommendation, got {len(rows)}"
    return rows[0]


def _rca_text(recommendation: Recommendation) -> str:
    actions = " ".join(
        action["action"] for action in recommendation.recommended_actions
    )
    return f"{recommendation.root_cause} {actions}".lower()


def _wrapper(recommendation: Recommendation) -> dict[str, Any]:
    bundle: dict[str, Any] = recommendation.context_bundle
    return bundle


async def test_a_covered_alert_yields_an_rca_grounded_in_its_runbook(
    knowledge_pipeline: Pipeline,
) -> None:
    """The done-condition, verbatim: fire an order-service alert, read the
    recommendation row, find the runbook's content in it."""
    response = await knowledge_pipeline.post_alert(COVERED_ALERT)
    assert response.status_code == 202
    await knowledge_pipeline.drain()

    recommendation = await _one_recommendation(knowledge_pipeline)

    # A real analysis, not a fallback that never saw the context.
    assert recommendation.is_fallback is False
    assert recommendation.llm_provider == "openai"

    # THE SYSTEM's half, asserted exactly: retrieval ran, was grounded, and the
    # model was shown chunks from the right runbook.
    wrapper = _wrapper(recommendation)
    assert wrapper["retrieval"] is not None, "retrieval was never attempted"
    assert wrapper["retrieval"]["outcome"] == "grounded"
    shown = wrapper["bundle"]["retrieved_context"]
    assert shown, "grounded but the bundle carried no chunks"
    assert {chunk["runbook_id"] for chunk in shown} == {"order-service-high-memory"}
    assert all(chunk.get("grade") for chunk in shown), (
        "chunks reached the model ungraded — CRAG degraded and nobody noticed"
    )

    # THE MODEL's half, asserted generously: the RCA references content that
    # exists only in the runbook.
    text = _rca_text(recommendation)
    assert any(token in text for token in RUNBOOK_TOKENS), (
        f"the RCA references none of the runbook's distinctive content "
        f"{RUNBOOK_TOKENS}; got root_cause={recommendation.root_cause!r} — "
        f"grounded context was shown and ignored"
    )


async def test_a_no_coverage_alert_yields_an_honestly_ungrounded_rca(
    knowledge_pipeline: Pipeline,
) -> None:
    """The other half of the phase's claim, and the reason CRAG survived its
    earn-its-keep gate: when no runbook fits, the RCA says so instead of being
    grounded in the least-bad wrong one."""
    response = await knowledge_pipeline.post_alert(NO_COVERAGE_ALERT)
    assert response.status_code == 202
    await knowledge_pipeline.drain()

    recommendation = await _one_recommendation(knowledge_pipeline)
    assert recommendation.is_fallback is False

    wrapper = _wrapper(recommendation)
    # EMPTY, and specifically not UNAVAILABLE: the grader ran and judged. An
    # unavailable here would mean the pipeline was down and the test proved
    # nothing about grading.
    assert wrapper["retrieval"] is not None, "retrieval was never attempted"
    assert wrapper["retrieval"]["outcome"] == "empty", (
        f"expected CRAG to judge nothing relevant, got "
        f"{json.dumps(wrapper['retrieval'])}"
    )
    assert wrapper["bundle"]["retrieved_context"] == []

    # The prompt's empty-context rule, through the whole pipeline: the RCA
    # admits the gap rather than inventing a runbook to fill it.
    text = _rca_text(recommendation)
    assert "runbook" in text and any(
        phrase in text for phrase in ("no runbook", "not cover", "do not cover")
    ), (
        f"the RCA does not admit the coverage gap: "
        f"root_cause={recommendation.root_cause!r}"
    )
