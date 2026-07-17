"""E3: the whole pipeline against REAL OpenAI — the POC gate, and the R0 answer.

Everything the deterministic e2e mocks, this runs for real: the actual ``llm-gateway``
app, keyed with a real OpenAI key, serving the reasoner's ``extended`` call end-to-end.
It is the last open question from R0 — *does a real extended call fit comfortably inside
the 60s reasoner budget, or is the margin tight?* — answered with a real number, not
an assumption.

OPT-IN, AND SKIPPED THREE WAYS
------------------------------
It hits a paid external API and is nondeterministic, so it never runs by accident:

- the ``live`` marker is deselected by default (``addopts = -m 'not live'``), so the
  normal suite, the pre-commit hook and CI skip it;
- running it is explicit — ``pytest -m live``;
- and even then it SKIPS without ``OPENAI_API_KEY``, so opting in without a key gives a
  skip, not a red build.

CONTRACT, NOT CONTENT
---------------------
The model's words are its own; asserting on them would be asserting on the weather. So
every assertion is on SHAPE — a real (non-fallback) recommendation, from the openai
provider, a gpt model, a confidence in the closed set, at least one action, a non-empty
root cause, and the correlation chain intact. Nothing about what the RCA actually says.

THE MEASUREMENT
---------------
The point of running it is the latency, so the pipeline is driven THREE times and the
per-call figures the gateway measured are reported as a small distribution — one sample
can mislead the "is the margin comfortable" judgement badly. The only latency ASSERTION
is loose (< the 60s budget, i.e. it did not fall back on timeout); the number itself is
printed for a human to read, not pinned. Run with ``-s`` to see it:

    pytest -m live -s tests/e2e/test_live_pipeline.py
"""

from __future__ import annotations

import asyncio

import pytest
from radar_database import Recommendation
from sqlalchemy import select

from tests.e2e.harness import MOCK_ALERT, Pipeline

pytestmark = pytest.mark.live

#: Real extended calls take seconds to tens of seconds; the budget is 60s and the worker
#: waits 90s. This guard sits well above both, so a genuinely slow call is measured
#: rather than cut off, while a truly hung run still cannot wedge the suite.
LIVE_GUARD_SECONDS = 180.0

REASONER_BUDGET_MS = 60_000
WORKER_TIMEOUT_MS = 90_000

RUNS = 3


async def test_live_openai_pipeline_and_latency(live_pipeline: Pipeline) -> None:
    """Drive the real pipeline ``RUNS`` times; assert the contract, report the latency.

    Each run uses a DISTINCT alert name so ingestion opens a fresh incident (the same
    alert within five minutes would deduplicate and produce no second analysis).
    """
    for i in range(RUNS):
        alert = {**MOCK_ALERT, "alert_name": f"LiveProbe{i}"}
        async with asyncio.timeout(LIVE_GUARD_SECONDS):
            response = await live_pipeline.post_alert(alert)
            assert response.status_code == 202
            await live_pipeline.drain()

    async with live_pipeline.db.session() as session:
        recommendations = list(await session.scalars(select(Recommendation)))

    assert len(recommendations) == RUNS, "each distinct alert should yield one RCA"

    latencies: list[int] = []
    for rec in recommendations:
        # CONTRACT, not content: a real analysis came back from OpenAI.
        assert rec.is_fallback is False, (
            "the real gateway fell back — the LLM path did not complete"
        )
        assert rec.llm_provider == "openai"
        assert rec.model_id.startswith("gpt")
        assert rec.confidence in {"low", "medium", "high"}
        assert rec.recommended_actions, "a real RCA has at least one action"
        assert rec.root_cause, "a real RCA has a non-empty root cause"
        assert rec.correlation_id is not None

        assert rec.latency_ms is not None
        # The only latency ASSERTION: it beat the budget (no timeout fallback).
        assert rec.latency_ms < REASONER_BUDGET_MS
        latencies.append(rec.latency_ms)

    _report_latency(latencies)


def _report_latency(latencies: list[int]) -> None:
    """Print the extended-call latency distribution and its margin. The R0 answer."""
    ordered = sorted(latencies)
    low, median, high = ordered[0], ordered[len(ordered) // 2], ordered[-1]
    worst_margin = REASONER_BUDGET_MS - high

    print("\n" + "=" * 66)
    print(f"  LIVE extended-call latency (real OpenAI, n={len(latencies)})")
    print("=" * 66)
    print(f"  samples (ms) : {ordered}")
    print(f"  min / median / max (ms) : {low} / {median} / {high}")
    print(
        f"  reasoner budget : {REASONER_BUDGET_MS} ms   worker timeout : "
        f"{WORKER_TIMEOUT_MS} ms"
    )
    print(
        f"  worst-case headroom under budget : {worst_margin} ms "
        f"({100 * worst_margin // REASONER_BUDGET_MS}%)"
    )
    print("=" * 66)
