"""E2: every fallback trigger, driven through the WHOLE pipeline to a real scrape.

This closes the gap R7d named out loud. Those tests ran against an unreachable gateway,
so the only fallback reason they ever observed through a real ``/metrics`` scrape was
``gateway_unavailable``; ``rejected``, ``not_json`` and ``schema_invalid`` were pinned
at the object and row level but never seen come out of the counter through the full HTTP
path. A reason mapped correctly in a unit test but mislabelled somewhere between the
handler and the metric would have passed every test in the suite — and mislabelling is
the one failure that matters here, because the label is what tells an operator whether
to fix a config (``rejected``) or wait for a provider (``gateway_unavailable``).

So each trigger is driven end-to-end — a real alert, the real worker, the reasoner
calling a real gateway socket — and the assertion is on the LABELLED count for that
reason, plus the absence of every other label. A total-count assertion would pass even
if every fallback were stamped ``gateway_unavailable``; that is precisely the bug this
file exists to rule out.

The ``timeout`` trigger runs against a COMPRESSED budget (see the harness), so it fires
in about a second rather than sixty, and every test is wrapped in an outer timeout so
the suite can never hang waiting on a mock that misbehaves.
"""

from __future__ import annotations

import asyncio

import pytest
from radar_database import Recommendation
from sqlalchemy import func, select

from tests.e2e import harness
from tests.e2e.harness import Pipeline

FALLBACK_TOTAL = "radar_recommendations_fallback_total"

#: Every reason label the reasoner can emit — the closed vocabulary this file pins.
ALL_REASONS = {
    "gateway_unavailable",
    "rejected",
    "timeout",
    "not_json",
    "schema_invalid",
}

#: (reason, gateway responder, delay). The delay is only non-zero for the timeout
#: trigger, whose whole point is to outrun the compressed budget.
TRIGGERS = [
    pytest.param(
        "gateway_unavailable", harness.unavailable, 0.0, id="gateway_unavailable"
    ),
    pytest.param("rejected", harness.rejected, 0.0, id="rejected"),
    pytest.param("not_json", harness.prose, 0.0, id="not_json"),
    pytest.param("schema_invalid", harness.bad_shape, 0.0, id="schema_invalid"),
    pytest.param(
        "timeout", harness.success, harness.TIMEOUT_DELAY_SECONDS, id="timeout"
    ),
]

# The outer guard: no e2e test may take longer than this. A real timeout fires at the
# compressed budget (~1s); anything approaching this bound means something hung.
GUARD_SECONDS = 15.0


@pytest.mark.parametrize(("reason", "responder", "delay"), TRIGGERS)
async def test_a_trigger_is_counted_under_its_own_reason(
    pipeline: Pipeline,
    reason: str,
    responder: harness.Responder,
    delay: float,
) -> None:
    """Each trigger produces a fallback counted under ITS reason, and no other.

    The two assertions are the whole point:

    - the labelled count for ``reason`` is 1 — the reason survived the full path to the
      metric, not just a unit-test mapping;
    - every OTHER reason label is absent — so a mislabelling that stamped, say, a parse
      failure as ``gateway_unavailable`` would fail here even though the count is right.
    """
    pipeline.mock.respond = responder
    pipeline.mock.delay_seconds = delay

    async with asyncio.timeout(GUARD_SECONDS):
        response = await pipeline.post_alert()
        assert response.status_code == 202
        await pipeline.drain()

    scrape = await pipeline.scrape("reasoner-agent")

    assert f'{FALLBACK_TOTAL}{{reason="{reason}"}} 1.0' in scrape, (
        f"expected a fallback counted under reason={reason!r}"
    )
    for other in ALL_REASONS - {reason}:
        assert f'{FALLBACK_TOTAL}{{reason="{other}"}}' not in scrape, (
            f"a {reason} fallback was mislabelled {other!r}"
        )


@pytest.mark.parametrize(("reason", "responder", "delay"), TRIGGERS)
async def test_the_stored_row_agrees_with_the_metric(
    pipeline: Pipeline,
    reason: str,
    responder: harness.Responder,
    delay: float,
) -> None:
    """The recommendation ROW records the same reason the counter did.

    Belt to the metric's suspenders: the counter and the stored ``context_bundle`` are
    written from the same outcome, and a test that only read the metric could miss a row
    that disagreed with it. Both come out of one alert, through the whole pipeline.
    """
    pipeline.mock.respond = responder
    pipeline.mock.delay_seconds = delay

    async with asyncio.timeout(GUARD_SECONDS):
        await pipeline.post_alert()
        await pipeline.drain()

    async with pipeline.db.session() as session:
        count = await session.scalar(select(func.count()).select_from(Recommendation))
        rec = (await session.scalars(select(Recommendation))).one()

    assert count == 1
    assert rec.is_fallback is True
    assert rec.llm_provider == "none"
    assert rec.confidence == "low"
    assert rec.context_bundle["fallback"]["reason"] == reason


async def test_a_success_is_not_counted_as_a_fallback(pipeline: Pipeline) -> None:
    """The control: a usable completion produces NO fallback series at all.

    Without this, "the fallback counter is empty" could mean the metric is broken rather
    than that nothing fell back. The default gateway returns a valid RCA, so this is the
    one case that must leave the fallback counter untouched.
    """
    async with asyncio.timeout(GUARD_SECONDS):
        await pipeline.post_alert()
        await pipeline.drain()

    scrape = await pipeline.scrape("reasoner-agent")
    for reason in ALL_REASONS:
        assert f'{FALLBACK_TOTAL}{{reason="{reason}"}}' not in scrape

    async with pipeline.db.session() as session:
        rec = (await session.scalars(select(Recommendation))).one()
    assert rec.is_fallback is False
    assert rec.llm_provider == "mock-openai"
