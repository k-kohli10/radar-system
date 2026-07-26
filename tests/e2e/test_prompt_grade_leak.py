"""Non-empty context must not produce empty-context language. Real model, real prompt.

THE REGRESSION
--------------
The reasoner used to dump the whole context bundle into the user message, leaking each
chunk's ``grade`` and ``status``, and the v2 system prompt told the model to "weight
excerpts graded sufficient over those graded partial". Section-level chunking makes
``sufficient`` unreachable in this corpus — every kept chunk grades ``partial`` — so
that instruction told the model, on every incident, that all of its context was the
weaker kind. It sometimes answered by taking the EMPTY-context path ("the root cause
of the incident is not covered by any specific runbook") with five sections of the
right runbook in the bundle.

This test replays the exact incident that did it: the metadata, plan steps, and five
graded chunks are the ones recovered from the stored ``context_bundle`` of the
recommendation that exhibited the symptom.

WHAT A GREEN RUN HERE DOES AND DOES NOT PROVE
----------------------------------------------
The symptom is a stochastic bias, and it was measured before this test was written:
on this bundle, the pre-fix prompt reproduced the empty-context language in **1 of 20**
draws and the fixed prompt in **0 of 40**. At a ~5% base rate, :data:`DRAWS` draws
catch a reintroduction roughly a third of the time — so this is CONFIRMATION, not the
guard.

The guard is deterministic and lives with the reasoner:
``apps/reasoner-agent/tests/test_prompt_context_rendering.py`` asserts that no grade
reaches the rendered prompt and that the system prompt says nothing about grades. Both
fail every time the fix is undone. Do not weaken them because this passed.

WHY THIS FILE IS UNDER tests/e2e/ AND NOT WITH THE REASONER'S OWN TESTS
-----------------------------------------------------------------------
``addopts = -m 'not live and not infra'`` lives in the ROOT pyproject, and
``apps/reasoner-agent/pyproject.toml`` declares its own ``[tool.pytest.ini_options]``
without it. Running ``pytest apps/reasoner-agent/tests/...`` therefore makes the app
the rootdir, the root addopts does not apply, and a ``live``-marked test sitting there
would fire real paid calls in an ordinary run — silently, since the marker would not
even be registered. Under ``tests/e2e/`` the root config always wins, which is what
makes the opt-in real.

Needs only the llm-gateway (no Elasticsearch, no Postgres)::

    pytest tests/e2e/test_prompt_grade_leak.py -m live
"""

from __future__ import annotations

import os
import re
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest
import pytest_asyncio
from pydantic import SecretStr
from radar_contracts import PlanStep, Severity
from radar_reasoner_agent.context import ContextBundle
from radar_reasoner_agent.fallback import RetrievalMetadata, resolve
from radar_reasoner_agent.llm import GatewayClient

pytestmark = [pytest.mark.live]

#: Defaults to the port `make gateway` serves, so the common local flow needs no
#: override. An EXPLICIT RADAR_GATEWAY_URL that is unreachable FAILS rather than skips
#: — a skip there would read as "opted out" when the truth is "misconfigured".
GATEWAY_URL = os.environ.get("RADAR_GATEWAY_URL", "http://127.0.0.1:8081")
GATEWAY_URL_EXPLICIT = "RADAR_GATEWAY_URL" in os.environ
SECRETS = Path.home() / ".radar-dev/secrets/reasoner-agent"

RUNBOOK = (
    Path(__file__).resolve().parents[2] / "docs/runbooks/order-service-high-memory.md"
)
RUNBOOK_ID = "order-service-high-memory"
RUNBOOK_TITLE = "Order Service High Memory"

#: The five sections real retrieval returned for this incident, in its order. The same
#: reconstruction the reasoner's unit test uses — deliberately duplicated rather than
#: imported, because an app's test module is not importable from here and a shared
#: fixture would make the deterministic guard depend on the e2e package.
RETRIEVED_SECTIONS = (
    "Investigation",
    "Symptoms",
    "Resolution",
    "Likely Causes",
    "Summary",
)

#: What every chunk in this corpus carries, and what used to reach the model.
CHUNK_GRADE = "partial"
CHUNK_STATUS = "fixture"

#: The empty-context language: CORRECT when the slot is empty, a regression when it is
#: not. Deliberately narrow — a grounded RCA may legitimately say "the runbook does not
#: cover the case where…", so widening this to every negation would fail on good
#: answers. These three are the phrasings the bug actually produced.
EMPTY_CONTEXT_PHRASES = ("not covered", "no specific runbook", "no runbook")

#: Content that exists in this runbook and not in the model's generic knowledge of
#: "high memory". Presence of any is what separates a grounded answer from generic
#: advice that merely avoids the phrases above.
RUNBOOK_TOKENS = (
    "1.5",
    "2gb",
    "2 gb",
    "oom",
    "plateau",
    "ecommerce",
    "restarts",
    "order_service_memory_bytes",
    "unbounded",
    "trough",
)

#: Draws per run — the size of the stored sample the bug first appeared in (1 of 8),
#: and about the most worth spending on a ~5% signal. See the module docstring for why
#: raising it does not turn this into the guard.
DRAWS = 8


def _graded_chunks() -> list[dict[str, Any]]:
    """The five real chunks, still carrying the bookkeeping the prompt must not show.

    Reconstructed from the corpus: chunk text is ``f"{title} — {section}\\n\\n{body}"``,
    verified byte for byte against the captured bundle. The non-empty guard matters —
    an empty fixture would make every assertion below pass while testing nothing.
    """
    assert RUNBOOK.exists(), f"the runbook fixture moved: {RUNBOOK}"
    body = RUNBOOK.read_text().split("---", 2)[2]
    sections = {
        match.group(1).strip(): match.group(2).strip()
        for match in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    }
    missing = [name for name in RETRIEVED_SECTIONS if not sections.get(name)]
    assert not missing, f"{RUNBOOK.name} no longer has sections {missing}"
    return [
        {
            "runbook_id": RUNBOOK_ID,
            "title": RUNBOOK_TITLE,
            "section": section,
            "content": f"{RUNBOOK_TITLE} — {section}\n\n{sections[section]}",
            "grade": CHUNK_GRADE,
            "status": CHUNK_STATUS,
        }
        for section in RETRIEVED_SECTIONS
    ]


def _bundle(chunks: list[dict[str, Any]]) -> ContextBundle:
    """The stored incident, verbatim: OrderServiceHighMemory on order-service."""
    return ContextBundle(
        incident_id=UUID("109e2239-b35d-485c-9a56-4a8509f789e7"),
        service_name="order-service",
        alert_name="OrderServiceHighMemory",
        severity=Severity.MEDIUM,
        opened_at=datetime(2026, 7, 26, 8, 16, 33, tzinfo=UTC),
        alert_count=1,
        investigation_steps=[
            PlanStep(
                order=1, description="Check order-service memory trend over last hour"
            ),
            PlanStep(
                order=2, description="Review heap dump or memory profile if available"
            ),
            PlanStep(
                order=3,
                description="Check for memory leak indicators in recent deployments",
            ),
            PlanStep(
                order=4, description="Consider restarting pod if memory is critical"
            ),
        ],
        retrieved_context=chunks,
    )


@pytest_asyncio.fixture
async def gateway() -> AsyncIterator[GatewayClient]:
    """A real GatewayClient against the real llm-gateway, or skip."""
    token_path = SECRETS / "gateway_token"
    if not token_path.exists():
        pytest.skip(f"no gateway token at {token_path}")

    # timeout=None, the way the reasoner's lifespan builds it: asyncio owns the clock.
    http = httpx.AsyncClient(base_url=GATEWAY_URL, timeout=None)
    try:
        await http.get("/healthz")
    except httpx.HTTPError:
        await http.aclose()
        if GATEWAY_URL_EXPLICIT:
            pytest.fail(
                f"RADAR_GATEWAY_URL was set explicitly but {GATEWAY_URL} does not "
                f"answer — misconfiguration, not an opt-out"
            )
        pytest.skip(f"no llm-gateway at {GATEWAY_URL} (start it with `make gateway`)")

    yield GatewayClient(http, SecretStr(token_path.read_text().strip()))
    await http.aclose()


async def test_non_empty_context_never_yields_empty_context_language(
    gateway: GatewayClient,
) -> None:
    """Its own runbook was retrieved; the RCA must not announce that none was.

    The whole reasoner path on the real inputs: the real prompt, the real gateway, the
    real model, the real parser, the real fallback resolution.

    ``is_fallback`` and the runbook tokens are asserted alongside the phrases, and they
    are what give the phrase check teeth. The TEMPLATE fallback's root cause ("AI
    analysis unavailable. Manual investigation required for…") contains none of the
    forbidden phrases either, so a gateway outage would sail straight through a
    phrase-only assertion; so would a fluent answer that ignored the context entirely.
    The three together say: a model answered, it used the runbook, and it did not claim
    the runbook was absent.
    """
    chunks = _graded_chunks()
    bundle = _bundle(chunks)
    retrieval = RetrievalMetadata(outcome="grounded", chunk_count=len(chunks))

    for draw in range(1, DRAWS + 1):
        outcome = resolve(bundle, await gateway.complete(bundle), retrieval=retrieval)

        assert outcome.is_fallback is False, (
            f"draw {draw}: fell back to a template, so this draw proves nothing about "
            f"the prompt: {outcome.root_cause!r}"
        )

        root_cause = outcome.root_cause.lower()
        for phrase in EMPTY_CONTEXT_PHRASES:
            assert phrase not in root_cause, (
                f"draw {draw}: the RCA used empty-context language {phrase!r} with "
                f"{len(chunks)} sections of its own runbook in the bundle — "
                f"root_cause={outcome.root_cause!r}"
            )

        actions = " ".join(action.action for action in outcome.recommended_actions)
        text = f"{root_cause} {actions}".lower()
        assert any(token in text for token in RUNBOOK_TOKENS), (
            f"draw {draw}: the RCA references none of the runbook's distinctive "
            f"content {RUNBOOK_TOKENS} — the context was shown and ignored, which is "
            f"the same defect wearing different words. "
            f"root_cause={outcome.root_cause!r}"
        )
