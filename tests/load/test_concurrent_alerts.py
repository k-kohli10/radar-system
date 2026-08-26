"""Phase 13 load test: 100 concurrent alerts, no data loss, a representative latency.

Two things this proves that no single-alert e2e can:

1. **No data loss under load.** 100 distinct alerts fired concurrently must each
   become exactly one incident, one plan, and one recommendation — 100 of each,
   the outbox fully drained, nothing dead-lettered, and not one silent fallback
   standing in for a lost LLM call. A pipeline that drops, doubles, or crosses a
   hand-off under concurrency fails here.

2. **A representative pipeline latency.** ``radar_incident_duration_seconds`` (the
   incident-pipeline latency panel, deferred from Phase 10 step 12) is OBSERVED
   once per recommendation, from the incident's ``opened_at`` to the
   recommendation's ``created_at`` — both Postgres ``now()``, one clock. Phase 10's
   single-alert run gave only a best-case point; 100 alerts queueing through the
   outbox give a p50/p95/p99 distribution the panel can actually show. The test
   asserts the histogram recorded all 100 observations and prints the percentiles.

The mock gateway answers instantly, so what this measures is RADAR's own pipeline
and queueing latency, not the model's — which is exactly what the incident-pipeline
panel is for (the LLM has its own ``radar_llm_duration_seconds``).
"""

from __future__ import annotations

import asyncio
import math

from radar_database import (
    Incident,
    InvestigationPlan,
    OutboxEvent,
    Recommendation,
)
from sqlalchemy import func, select

from tests.e2e.harness import Pipeline

#: Concurrent alerts to fire. The plan's number.
ALERT_COUNT = 100

#: Metric family the incident-pipeline latency panel reads.
DURATION_METRIC = "radar_incident_duration_seconds"


def _distinct_alerts(n: int) -> list[dict[str, str]]:
    """``n`` alerts with distinct fingerprints — a distinct service each.

    A distinct ``service_name`` gives a distinct fingerprint (so ingestion opens a
    fresh incident per alert rather than deduplicating them onto one) and keeps each
    out of the watcher's service-groups, so the 100 stay 100 independent incidents.
    """
    return [
        {
            "service_name": f"loadgen-{i:03d}",
            "alert_name": "LoadTestFailure",
            "severity": "critical",
        }
        for i in range(n)
    ]


def _percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile of ``values`` (exact, from the per-incident spans)."""
    ordered = sorted(values)
    rank = max(0, math.ceil(pct / 100 * len(ordered)) - 1)
    return ordered[rank]


def _histogram_count(metrics_text: str, name: str) -> float:
    """The ``_count`` sample of a Prometheus histogram, 0.0 if the family is absent."""
    needle = f"{name}_count "
    for line in metrics_text.splitlines():
        if line.startswith(needle):
            return float(line[len(needle) :])
    return 0.0


async def test_100_concurrent_alerts_no_data_loss(pipeline: Pipeline) -> None:
    """Fire 100 alerts at once; every one must reach a recommendation intact."""
    alerts = _distinct_alerts(ALERT_COUNT)

    responses = await asyncio.gather(*(pipeline.post_alert(a) for a in alerts))
    incident_ids = {r.json()["incident_id"] for r in responses}
    assert all(r.status_code == 202 for r in responses), "ingestion rejected an alert"
    assert len(incident_ids) == ALERT_COUNT, "distinct alerts collapsed into fewer"

    # Drive the whole outbox to rest. Generous ceiling: ~4 hops x 100 alerts at a
    # batch of 20 is ~20 iterations; 500 leaves headroom without masking a stall.
    await pipeline.drain(max_iterations=500)

    async with pipeline.db.session() as session:
        incidents = await session.scalar(select(func.count()).select_from(Incident))
        plans = await session.scalar(
            select(func.count()).select_from(InvestigationPlan)
        )
        recs = list(await session.scalars(select(Recommendation)))
        remaining = list(await session.scalars(select(OutboxEvent)))

    # (1) No data loss: exactly one of each row per alert.
    assert incidents == ALERT_COUNT, (
        f"expected {ALERT_COUNT} incidents, got {incidents}"
    )
    assert plans == ALERT_COUNT, f"expected {ALERT_COUNT} plans, got {plans}"
    assert len(recs) == ALERT_COUNT, f"expected {ALERT_COUNT} recommendations"
    # Every posted incident got its OWN recommendation — no crossed or dropped hand-off.
    assert {str(r.incident_id) for r in recs} == incident_ids
    # The queue emptied: nothing left pending, nothing dead-lettered.
    assert remaining == [], (
        f"outbox did not drain: {[(e.event_type, e.status) for e in remaining]}"
    )
    # Not one recommendation is a template standing in for a lost LLM call — the mock
    # answered every time, so a fallback here would mean a real failure was masked.
    fallbacks = [r for r in recs if r.is_fallback]
    assert not fallbacks, f"{len(fallbacks)} recommendations silently fell back"

    # (2) The incident-pipeline latency, off the same span the panel observes.
    async with pipeline.db.session() as session:
        spans = list(
            await session.execute(
                select(Recommendation.created_at, Incident.opened_at).join(
                    Incident, Recommendation.incident_id == Incident.id
                )
            )
        )
    latencies = [(created - opened).total_seconds() for created, opened in spans]
    assert len(latencies) == ALERT_COUNT
    assert all(latency >= 0 for latency in latencies), (
        "a recommendation predates its incident"
    )

    p50 = _percentile(latencies, 50)
    p95 = _percentile(latencies, 95)
    p99 = _percentile(latencies, 99)
    print(
        f"\nincident-pipeline latency over {ALERT_COUNT} concurrent alerts (seconds): "
        f"p50={p50:.3f} p95={p95:.3f} p99={p99:.3f} max={max(latencies):.3f}"
    )
    assert p50 <= p95 <= p99, "percentiles out of order"

    # The panel is populated: the histogram observed every recommendation's span.
    metrics_text = await pipeline.scrape("reasoner-agent")
    observed = _histogram_count(metrics_text, DURATION_METRIC)
    assert observed == ALERT_COUNT, (
        f"{DURATION_METRIC} recorded {observed} observations, expected {ALERT_COUNT} — "
        "the latency panel would under-report"
    )
