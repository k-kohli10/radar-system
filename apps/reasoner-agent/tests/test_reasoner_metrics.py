"""The counters, and the one property that decides where they are incremented.

A metric that counts INTENTIONS rather than FACTS is worse than no metric, because it
is believed. The fallback counter feeds ``LLMFallbackRateHigh``; if it moved on writes
that later rolled back, the alert would fire on recommendations that do not exist, and
the first thing anyone would do is stop trusting the alert.

So every increment happens strictly AFTER the commit, and
``test_a_rolled_back_write_does_not_move_the_counter`` is what makes that placement a
tested property rather than a convention someone can quietly break.

Everything here runs against the UNREACHABLE gateway, so every recommendation is a
``gateway_unavailable`` fallback. That is the honest limit of these tests: they pin the
fallback and duplicate paths end-to-end, and the success path's provider/confidence
labels are covered at the object level in test_fallback and the row level in
test_storage.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
import pytest_asyncio
from prometheus_client import CollectorRegistry
from radar_common import AGENT_TOKEN_HEADER, new_id
from radar_contracts import ReasoningRequestedPayload
from radar_database import Alert, Database, Incident, InvestigationPlan, Recommendation
from radar_reasoner_agent import routes as routes_module
from radar_reasoner_agent import storage
from radar_reasoner_agent.main import create_app
from sqlalchemy import func, select

TOKEN = "r" * 64
UNREACHABLE_GATEWAY = "http://127.0.0.1:1"

FALLBACK_TOTAL = "radar_recommendations_fallback_total"
RECOMMENDATIONS_TOTAL = "radar_recommendations_total"
DUPLICATES_TOTAL = "radar_duplicate_recommendation_requests_total"
DURATION_COUNT = "radar_incident_duration_seconds_count"
DURATION_SUM = "radar_incident_duration_seconds_sum"


@pytest_asyncio.fixture
async def client(
    db: Database, database_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[httpx.AsyncClient]:
    (tmp_path / "postgres_dsn").write_text(database_url)
    (tmp_path / "agent_token").write_text(TOKEN)
    (tmp_path / "gateway_token").write_text("g" * 64)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path))
    monkeypatch.setenv("RADAR_GATEWAY_URL", UNREACHABLE_GATEWAY)

    app = create_app(metrics_registry=CollectorRegistry(), with_tracing=False)
    async with app.router.lifespan_context(app):
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(
            transport=transport, base_url="http://reasoner"
        ) as http:
            yield http


async def _seed(db: Database) -> tuple[UUID, UUID, UUID]:
    correlation_id = uuid4()
    incident = Incident(
        id=uuid4(),
        correlation_id=correlation_id,
        fingerprint="f" * 64,
        service_name="order-service",
        title="order-service OrderProcessingFailureRate",
        severity="critical",
        status="open",
        alert_count=1,
    )
    plan = InvestigationPlan(
        id=uuid4(),
        incident_id=incident.id,
        correlation_id=correlation_id,
        steps=[{"order": 1, "description": "Check recent deployments"}],
        template_key="order-service:OrderProcessingFailureRate",
        status="pending",
    )
    async with db.session() as session:
        session.add(incident)
        session.add(
            Alert(
                id=uuid4(),
                source="mock",
                fingerprint="f" * 64,
                service_name="order-service",
                alert_name="OrderProcessingFailureRate",
                severity="critical",
                status="firing",
                raw_payload={},
                fired_at=datetime.now(UTC),
                received_at=datetime.now(UTC),
                incident_id=incident.id,
                correlation_id=correlation_id,
            )
        )
        session.add(plan)
        await session.commit()
    return incident.id, plan.id, correlation_id


def _envelope(incident_id: UUID, plan_id: UUID, correlation_id: UUID) -> dict[str, Any]:
    return {
        "event_id": str(new_id()),
        "event_type": "incident.reasoning_requested",
        "correlation_id": str(correlation_id),
        "payload": ReasoningRequestedPayload(
            incident_id=incident_id, plan_id=plan_id
        ).model_dump(mode="json"),
    }


async def _scrape(client: httpx.AsyncClient) -> str:
    return (await client.get("/metrics")).text


def _unlabelled_sample(scrape: str, name: str) -> float | None:
    """The value of the unlabelled sample line ``name`` in a scrape, or ``None``."""
    for line in scrape.splitlines():
        if line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) == 2 and parts[0] == name:
            return float(parts[1])
    return None


async def _post(client: httpx.AsyncClient, body: dict[str, Any]) -> httpx.Response:
    return await client.post("/events", json=body, headers={AGENT_TOKEN_HEADER: TOKEN})


# --- the fallback counter, and its reason label -------------------------------


async def test_a_fallback_is_counted_under_its_own_reason(
    client: httpx.AsyncClient, db: Database
) -> None:
    """The gateway is unreachable, so this is a ``gateway_unavailable`` fallback.

    The LABEL is the point. ``rejected`` means fix the config now; this one means the
    provider is down and waiting is correct. An unlabelled total cannot tell an operator
    which, and the two demand opposite responses.
    """
    incident_id, plan_id, correlation_id = await _seed(db)

    response = await _post(client, _envelope(incident_id, plan_id, correlation_id))
    assert response.status_code == 200

    scrape = await _scrape(client)
    assert f'{FALLBACK_TOTAL}{{reason="gateway_unavailable"}} 1.0' in scrape
    # And NOT under a reason that did not happen — the label is not decorative.
    assert f'{FALLBACK_TOTAL}{{reason="rejected"}}' not in scrape
    # A fallback is still a recommendation, and it names no provider.
    assert f'{RECOMMENDATIONS_TOTAL}{{confidence="low",provider="none"}} 1.0' in scrape


async def test_a_rolled_back_write_does_not_move_the_counter(
    client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE property that puts the increment after the commit.

    The write fails at the marker, so the whole transaction — recommendation, outbox
    event, audit row — rolls back. Nothing was stored, so nothing may be counted. If the
    counter moved anyway, the fallback RATE would be inflated by writes that do not
    exist, and ``LLMFallbackRateHigh`` would fire on phantom recommendations.

    Mutation that must turn this red: move the increment above ``session.commit()``.
    The counter then moves for a recommendation that was never written.
    """
    incident_id, plan_id, correlation_id = await _seed(db)

    async def exploding_marker(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the marker failed; the transaction must roll back")

    monkeypatch.setattr(routes_module, "mark_processed", exploding_marker)

    with pytest.raises(RuntimeError, match="the marker failed"):
        await _post(client, _envelope(incident_id, plan_id, correlation_id))

    # Nothing was written...
    async with db.session() as session:
        stored = await session.scalar(select(func.count()).select_from(Recommendation))
    assert stored == 0

    # ...so nothing was counted. Not even a zero-valued series: the label combination
    # was never observed, so an alert cannot fire off a series that never existed.
    scrape = await _scrape(client)
    assert FALLBACK_TOTAL not in scrape.replace(f"# HELP {FALLBACK_TOTAL}", "").replace(
        f"# TYPE {FALLBACK_TOTAL}", ""
    ), "a recommendation that rolled back was counted — the counter is now a phantom"


# --- the duplicate counter, on BOTH paths -------------------------------------


async def test_the_sequential_duplicate_is_counted(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Two distinct events, one incident, delivered one after the other."""
    incident_id, plan_id, correlation_id = await _seed(db)

    first = await _post(client, _envelope(incident_id, plan_id, correlation_id))
    second = await _post(client, _envelope(incident_id, plan_id, correlation_id))

    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "already_recommended"

    scrape = await _scrape(client)
    assert f"{DUPLICATES_TOTAL} 1.0" in scrape
    # The duplicate produced no recommendation, so it must not inflate the totals.
    assert f'{FALLBACK_TOTAL}{{reason="gateway_unavailable"}} 1.0' in scrape


async def test_the_concurrent_duplicate_is_counted_too(
    client: httpx.AsyncClient, db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race path counts as well — both duplicate paths, or the number is a lie.

    A counter wired to only one of the two guards would under-report duplicates by
    exactly the cases that are hardest to reproduce.
    """
    incident_id, plan_id, correlation_id = await _seed(db)

    barrier = asyncio.Barrier(2)
    real_lookup = storage._existing_recommendation_id

    async def gated(session: Any, incident: UUID) -> UUID | None:
        found = await real_lookup(session, incident)
        await barrier.wait()
        return found

    monkeypatch.setattr(storage, "_existing_recommendation_id", gated)

    responses = await asyncio.gather(
        *(
            _post(client, _envelope(incident_id, plan_id, correlation_id))
            for _ in range(2)
        )
    )

    assert sorted(r.json()["status"] for r in responses) == [
        "already_recommended",
        "processed",
    ]

    scrape = await _scrape(client)
    assert f"{DUPLICATES_TOTAL} 1.0" in scrape
    # One winner, one recommendation, counted once.
    assert f'{FALLBACK_TOTAL}{{reason="gateway_unavailable"}} 1.0' in scrape


# --- the ingestion-to-recommendation latency histogram ------------------------


async def test_incident_duration_is_observed_once_per_recommendation(
    client: httpx.AsyncClient, db: Database
) -> None:
    """The pipeline latency lands exactly once, and only for a real WRITE.

    It is observed at RECOMMENDATION creation, NOT at resolution — the incident here is
    never resolved, so the old "open-to-resolution" reading would record nothing at all.
    A count of exactly 1 is what proves the observation is keyed to the recommendation;
    the positive, sub-minute sum is what proves it measures the incident's ``opened_at``
    against the recommendation's ``created_at`` (a real span on one DB clock) rather
    than a zero (created_at - created_at) or an unbounded human-loop duration.

    Mutation that must turn this red: measure ``created_at - created_at`` (the sum goes
    to 0.0), or observe open-to-resolution (nothing observed and the count is absent).
    """
    incident_id, plan_id, correlation_id = await _seed(db)

    response = await _post(client, _envelope(incident_id, plan_id, correlation_id))
    assert response.status_code == 200

    scrape = await _scrape(client)
    assert f"{DURATION_COUNT} 1.0" in scrape
    total = _unlabelled_sample(scrape, DURATION_SUM)
    assert total is not None and 0.0 < total < 60.0, (
        f"expected a positive, sub-minute pipeline latency, got {total}"
    )


async def test_a_duplicate_adds_no_second_duration_observation(
    client: httpx.AsyncClient, db: Database
) -> None:
    """Only a genuine recommendation is a data point.

    The second event is a duplicate (one RCA per incident): it stores nothing, so it
    must not add a latency observation — otherwise the histogram would count events, not
    recommendations, and the average latency would be diluted by zero-work duplicates.

    Mutation that must turn this red: move the observe above the duplicate early-return
    in the route, so the second (no-write) event observes a second point.
    """
    incident_id, plan_id, correlation_id = await _seed(db)

    first = await _post(client, _envelope(incident_id, plan_id, correlation_id))
    second = await _post(client, _envelope(incident_id, plan_id, correlation_id))
    assert first.json()["status"] == "processed"
    assert second.json()["status"] == "already_recommended"

    scrape = await _scrape(client)
    assert f"{DURATION_COUNT} 1.0" in scrape, "a duplicate must not add a data point"
