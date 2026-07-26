"""The knowledge client, and the three-way outcome riding to the stored bundle.

Everything upstream — CRAG internally, the context API at its HTTP boundary —
kept "the corpus has nothing" apart from "retrieval is down". This is the last
layer that can collapse them, so the tests here are organised around the
distinction surviving: through the client's outcome, through the route's
handling, into ``recommendations.context_bundle``.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from pydantic import SecretStr
from radar_common import AGENT_TOKEN_HEADER, ConfigurationError, EventsAuth, new_id
from radar_common.timeouts import (
    REASONER_DISPATCH_TIMEOUT_SECONDS,
    REASONER_KNOWLEDGE_BUDGET_SECONDS,
    REASONER_LLM_BUDGET_SECONDS,
)
from radar_contracts import ReasoningRequestedPayload
from radar_database import Alert, Database, Incident, InvestigationPlan, Recommendation
from radar_reasoner_agent.context import ContextBundle
from radar_reasoner_agent.knowledge import (
    KnowledgeClient,
    KnowledgeResult,
    RetrievalOutcome,
)
from radar_reasoner_agent.llm import LLMSuccess
from radar_reasoner_agent.routes import create_events_router
from radar_telemetry import create_reasoner_metrics
from radar_testing.postgres import database_url, db  # noqa: F401  (shared fixtures)
from sqlalchemy import select

pytestmark = pytest.mark.asyncio

KNOWLEDGE_TOKEN = SecretStr("k" * 64)
AGENT_TOKEN = "r" * 64

GOOD_RCA = (
    '{"root_cause": "The cache invalidation storm overloaded the database.", '
    '"confidence": "high", '
    '"recommended_actions": [{"order": 1, "action": "Warm the cache gradually"}]}'
)

CHUNK = {
    "runbook_id": "inventory-cache-invalidation-storm",
    "title": "Inventory Cache Invalidation Storm",
    "section": "Resolution",
    "content": "Warm the cache gradually rather than all at once.",
    "grade": "sufficient",
}


def _bundle() -> ContextBundle:
    return ContextBundle(
        incident_id=uuid4(),
        service_name="inventory-service",
        alert_name="InventoryCheckLatency",
        severity="high",
        opened_at=datetime.now(UTC),
        alert_count=1,
        investigation_steps=[],
        retrieved_context=[],
    )


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://ks", timeout=None
    )


# -------------------------------------------------------------------- client


async def test_populated_chunks_are_grounded() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"chunks": [CHUNK]})

    async with _client(handler) as http:
        result = await KnowledgeClient(http, KNOWLEDGE_TOKEN).fetch(_bundle())

    assert result.outcome is RetrievalOutcome.GROUNDED
    assert result.chunks == [CHUNK]
    # The caller presents the TARGET's token — the dispatch_tokens rule.
    assert requests[0].headers[AGENT_TOKEN_HEADER] == KNOWLEDGE_TOKEN.get_secret_value()


async def test_an_empty_200_is_empty_not_unavailable() -> None:
    """CRAG's judgment. Mistaking it for an outage would discard the one output
    the grading stage exists to produce."""
    async with _client(lambda _: httpx.Response(200, json={"chunks": []})) as http:
        result = await KnowledgeClient(http, KNOWLEDGE_TOKEN).fetch(_bundle())

    assert result.outcome is RetrievalOutcome.EMPTY
    assert result.chunks == []
    assert result.detail is None


@pytest.mark.parametrize("status", [401, 422, 500, 503])
async def test_a_non_200_is_unavailable_not_empty(status: int) -> None:
    """The API's 503 means retrieval failed. Reading it as 'no coverage' would
    manufacture CRAG's claim out of an outage — at the very last layer."""
    async with _client(lambda _: httpx.Response(status, json={})) as http:
        result = await KnowledgeClient(http, KNOWLEDGE_TOKEN).fetch(_bundle())

    assert result.outcome is RetrievalOutcome.UNAVAILABLE
    assert result.detail == f"HTTP {status}"


async def test_a_transport_failure_is_unavailable_with_the_class_only() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("http://user:secret@10.0.0.9 refused")

    async with _client(handler) as http:
        result = await KnowledgeClient(http, KNOWLEDGE_TOKEN).fetch(_bundle())

    assert result.outcome is RetrievalOutcome.UNAVAILABLE
    assert result.detail == "ConnectError"


async def test_a_malformed_200_is_unavailable() -> None:
    async with _client(lambda _: httpx.Response(200, json={"nope": 1})) as http:
        result = await KnowledgeClient(http, KNOWLEDGE_TOKEN).fetch(_bundle())

    assert result.outcome is RetrievalOutcome.UNAVAILABLE


async def test_the_fetch_never_raises_on_any_of_the_failure_paths() -> None:
    """The route calls fetch with no try/except, and that is only sound if this
    holds. One representative of each failure family, asserted not to raise."""
    for handler in (
        lambda _: httpx.Response(500, json={}),
        lambda _: httpx.Response(200, content=b"not json"),
    ):
        async with _client(handler) as http:
            result = await KnowledgeClient(http, KNOWLEDGE_TOKEN).fetch(_bundle())
        assert result.outcome is RetrievalOutcome.UNAVAILABLE


async def test_a_client_timeout_shorter_than_the_budget_is_refused() -> None:
    async with httpx.AsyncClient(timeout=5.0) as http:
        with pytest.raises(ConfigurationError, match="shorter than"):
            KnowledgeClient(http, KNOWLEDGE_TOKEN, budget_seconds=20.0)


def test_the_dispatch_invariant_covers_both_remote_calls() -> None:
    """The worker must outlast the SUM — the knowledge fetch and the LLM call
    are sequential on one dispatch. Pinned here so a future budget bump that
    breaks the ordering fails a named test, not just an import assert."""
    assert (
        REASONER_LLM_BUDGET_SECONDS + REASONER_KNOWLEDGE_BUDGET_SECONDS
        < REASONER_DISPATCH_TIMEOUT_SECONDS
    )


# ------------------------------------------- through the route, into storage


class FakeGateway:
    """Records the bundle it was shown — the assertion surface for grounding."""

    def __init__(self) -> None:
        self.bundles: list[ContextBundle] = []

    async def complete(self, bundle: ContextBundle) -> LLMSuccess:
        self.bundles.append(bundle)
        return LLMSuccess(
            content=GOOD_RCA,
            provider="openai",
            model="gpt-4o",
            mode="extended",
            prompt_tokens=100,
            completion_tokens=50,
            latency_ms=10,
        )


class FakeKnowledge:
    def __init__(self, result: KnowledgeResult) -> None:
        self.result = result

    async def fetch(self, bundle: ContextBundle) -> KnowledgeResult:
        return self.result


async def _seed(db: Database) -> tuple[UUID, UUID, UUID]:  # noqa: F811
    correlation_id = uuid4()
    incident = Incident(
        id=uuid4(),
        correlation_id=correlation_id,
        fingerprint="a" * 64,
        service_name="inventory-service",
        title="inventory-service InventoryCheckLatency",
        severity="high",
        status="open",
        alert_count=1,
    )
    plan = InvestigationPlan(
        id=uuid4(),
        incident_id=incident.id,
        correlation_id=correlation_id,
        steps=[{"order": 1, "description": "Check cache hit rate"}],
        template_key="inventory-service:InventoryCheckLatency",
    )
    async with db.session() as session:
        session.add(incident)
        session.add(
            Alert(
                id=uuid4(),
                source="mock",
                fingerprint="a" * 64,
                service_name="inventory-service",
                alert_name="InventoryCheckLatency",
                severity="high",
                status="firing",
                raw_payload={},
                incident_id=incident.id,
                correlation_id=correlation_id,
                fired_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
            )
        )
        session.add(plan)
        await session.commit()
    return incident.id, plan.id, correlation_id


def _app(db: Database, knowledge: FakeKnowledge | None) -> tuple[FastAPI, FakeGateway]:  # noqa: F811
    from radar_common import AgentTokenAuth

    gateway = FakeGateway()
    app = FastAPI()
    app.include_router(
        create_events_router(
            get_database=lambda: db,
            get_gateway=lambda: gateway,  # type: ignore[arg-type,return-value]
            get_knowledge=lambda: knowledge,  # type: ignore[arg-type,return-value]
            events_auth=EventsAuth(
                lambda: AgentTokenAuth([AGENT_TOKEN]), service_name="reasoner-agent"
            ),
            metrics=create_reasoner_metrics(CollectorRegistry()),
        )
    )
    return app, gateway


async def _post_event(app: FastAPI, incident_id: UUID, plan_id: UUID) -> None:
    envelope = {
        "event_id": str(new_id()),
        "event_type": "incident.reasoning_requested",
        "correlation_id": str(uuid4()),
        "payload": ReasoningRequestedPayload(
            incident_id=incident_id, plan_id=plan_id
        ).model_dump(mode="json"),
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://r"
    ) as client:
        response = await client.post(
            "/events", json=envelope, headers={AGENT_TOKEN_HEADER: AGENT_TOKEN}
        )
    assert response.status_code == 200, response.text


async def _stored_wrapper(db: Database, incident_id: UUID) -> dict[str, Any]:  # noqa: F811
    async with db.session() as session:
        row = await session.scalar(
            select(Recommendation).where(Recommendation.incident_id == incident_id)
        )
    assert row is not None
    bundle: dict[str, Any] = row.context_bundle
    return bundle


async def test_grounded_fills_the_bundle_the_model_sees(db: Database) -> None:  # noqa: F811
    """The phase's payoff, at the seam: the model is shown the graded chunks,
    and the stored wrapper records that grounding happened."""
    incident_id, plan_id, _ = await _seed(db)
    app, gateway = _app(
        db,
        FakeKnowledge(
            KnowledgeResult(
                outcome=RetrievalOutcome.GROUNDED, chunks=[CHUNK], elapsed_ms=42
            )
        ),
    )

    await _post_event(app, incident_id, plan_id)

    (shown,) = gateway.bundles
    assert shown.retrieved_context == [CHUNK]

    wrapper = await _stored_wrapper(db, incident_id)
    assert wrapper["retrieval"]["outcome"] == "grounded"
    assert wrapper["retrieval"]["chunk_count"] == 1
    assert wrapper["bundle"]["retrieved_context"] == [CHUNK]


async def test_empty_and_unavailable_stay_distinct_in_storage(db: Database) -> None:  # noqa: F811
    """Both leave retrieved_context empty on the bundle — identical to the
    model — so the sibling metadata is the ONLY record of which happened.
    Collapsing them here would waste the distinction every layer preserved."""
    for result, expected in (
        (KnowledgeResult(outcome=RetrievalOutcome.EMPTY), "empty"),
        (
            KnowledgeResult(outcome=RetrievalOutcome.UNAVAILABLE, detail="HTTP 503"),
            "unavailable",
        ),
    ):
        incident_id, plan_id, _ = await _seed(db)
        app, gateway = _app(db, FakeKnowledge(result))

        await _post_event(app, incident_id, plan_id)

        (shown,) = gateway.bundles
        assert shown.retrieved_context == []

        wrapper = await _stored_wrapper(db, incident_id)
        assert wrapper["retrieval"]["outcome"] == expected
        assert wrapper["bundle"]["retrieved_context"] == []
        if expected == "unavailable":
            assert wrapper["retrieval"]["detail"] == "HTTP 503"


async def test_no_knowledge_service_means_retrieval_was_never_attempted(  # noqa: F811
    db: Database,  # noqa: F811
) -> None:
    """Pre-Phase-8 behaviour, still supported: wrapper.retrieval is null —
    'never attempted' is a third thing, distinct from both empty and failed."""
    incident_id, plan_id, _ = await _seed(db)
    app, _ = _app(db, knowledge=None)

    await _post_event(app, incident_id, plan_id)

    wrapper = await _stored_wrapper(db, incident_id)
    assert wrapper["retrieval"] is None


def test_a_result_cannot_carry_chunks_it_does_not_vouch_for() -> None:
    """The invariant the route leans on, made structural.

    The route copies `chunks` onto the bundle the model reads whenever the
    outcome says grounded — and skips them otherwise, which is only safe if a
    non-grounded result CANNOT carry chunks. Without this, an `unavailable`
    result holding chunks would either ground an RCA in unvouched content or be
    silently dropped, depending on an if-statement; with it, the inconsistent
    value is unrepresentable.
    """
    with pytest.raises(ValueError, match="must carry no chunks"):
        KnowledgeResult(outcome=RetrievalOutcome.UNAVAILABLE, chunks=[CHUNK])

    with pytest.raises(ValueError, match="must carry chunks"):
        KnowledgeResult(outcome=RetrievalOutcome.GROUNDED, chunks=[])


# ----------------------------------------------------------------- the prompt


def test_the_prompt_pins_the_empty_context_rule() -> None:
    """The rule that makes CRAG's empty verdict matter, guarded against edits.

    A prompt is prose, and prose gets "tidied". These assertions are not about
    wording — they pin that the three load-bearing instructions EXIST: ground in
    the context when present, treat an empty slot as a fact about coverage, and
    never invent a runbook to fill it. Deleting any of them turns the empty
    path's honestly-ungrounded RCA back into plausible fabrication, silently,
    with every other test green.
    """
    from radar_reasoner_agent.llm import SYSTEM_PROMPT

    prompt = SYSTEM_PROMPT.lower()
    assert "retrieved_context" in prompt
    # Grounding when present…
    assert "ground your analysis" in prompt
    # …honesty when absent…
    assert "do not cover this incident" in prompt
    # …and the anti-fabrication boundary itself.
    assert "never invent" in prompt


# ``test_the_prompt_tells_the_model_to_weight_grades`` used to sit here, pinning the
# clause "weight excerpts graded sufficient over those graded partial". It was
# removed with the clause: section-level chunking makes `sufficient` structurally
# unreachable, so that instruction fired on every incident and told the model all of
# its context was the weaker kind — and the model answered with the EMPTY-context
# response while holding the right runbook. The grades never belonged in front of the
# model; they belong in the gate and the audit trail, which is where they still are.
# The inverse property is now pinned in ``test_prompt_context_rendering.py``, in
# ``test_the_system_prompt_says_nothing_about_grades``.
