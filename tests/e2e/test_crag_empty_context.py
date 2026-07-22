"""CRAG's earn-its-keep gate: can it say NO on genuine no-coverage?

Real Elasticsearch, real llm-gateway, real OpenAI — the full retrieval pipeline
with grading wired, the configuration the reasoner will consume.

THE GATE
--------
The stage's entire justification is that it can return an empty context when the
corpus has nothing for an incident, instead of handing the reasoner the closest
wrong runbook. Reranking faced the equivalent gate (reliably improve ranking at
n=20) and was cut when it failed. CRAG's is: on an incident whose failure NO
runbook covers, the grader must run, judge every retrieved chunk insufficient,
and yield an empty context. If it cannot say no even there, it is ceremony and
goes the way of reranking.

WHY THE NO-COVERAGE INCIDENT IS THE HARD CASE, NOT A SOFT ONE
--------------------------------------------------------------
The incident is scoped to ``inventory-service``, so the pre-filter guarantees
every retrieved chunk is about the RIGHT SERVICE. All four inventory runbooks
are stock-level problems (cache storm, check latency, oversell, reservation
leak); the incident is a data-handling failure — PII written to debug logs —
which none of them remotely covers. The grader therefore sees a page of
superficially-relevant excerpts and must reject every one. A grader with any
bias toward "right service, probably fine" fails here.

EMPTY FROM JUDGMENT, NOT FROM FAILURE — PROVEN, NOT ASSUMED
------------------------------------------------------------
An empty result caused by the grading call erroring out would look identical in
the return value while being exactly the lie the client's empty-vs-failure
distinction exists to prevent (a failed call returns chunks UNGRADED, so a
failure here would actually surface as a NON-empty ungraded result — but the
test does not lean on remembering that). Logs are captured and asserted:
``knowledge.graded`` fired with ``empty_context=True``, and
``knowledge.grading_skipped`` never fired.

BOTH DIRECTIONS, PER THE PHASE'S CHECKER DISCIPLINE
----------------------------------------------------
The empty case alone would pass for a grader that always says insufficient — as
useless as one that always says sufficient, and more destructive. So the same
pipeline is also given a COVERED incident and must return a non-empty, graded
context. Only the pair proves grading discriminates.

Reranking's lesson about single draws applies to the LLM here too, so the
no-coverage grading runs ``GATE_REPEATS`` times and every one must be empty.

Requires both markers' prerequisites::

    pytest tests/e2e/test_crag_empty_context.py -m "live and infra"
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Mapping, Sequence
from pathlib import Path
from typing import Any

import httpx
import pytest
import pytest_asyncio
import structlog
from pydantic import SecretStr
from radar_knowledge_service.crag_client import GatewayGrader
from radar_knowledge_service.embeddings import GatewayEmbeddingClient
from radar_knowledge_service.retrieval import HybridRetriever
from radar_plugin_knowledge_elastic import ElasticKnowledgeStore

pytestmark = [pytest.mark.live, pytest.mark.infra]

ES_URL = os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200")
#: Defaults to the port `make gateway` serves, so the common local flow needs
#: no override. An EXPLICIT RADAR_GATEWAY_URL that is unreachable FAILS rather
#: than skips — a skip there would read as "opted out" when the truth is
#: "misconfigured", the silent no-op this repo keeps designing against.
GATEWAY_URL = os.environ.get("RADAR_GATEWAY_URL", "http://127.0.0.1:8081")
GATEWAY_URL_EXPLICIT = "RADAR_GATEWAY_URL" in os.environ
SECRETS = Path.home() / ".radar-dev/secrets/knowledge-service"
INDEX = "radar-runbooks"

#: One draw proved nothing for reranking; it proves nothing here either. Every
#: repeat must come back empty for the gate to pass.
GATE_REPEATS = 3

#: Scoped to inventory-service so the pre-filter returns all-right-service
#: chunks, and about a failure class (data handling) that none of the four
#: inventory runbooks — all stock-level problems — covers.
NO_COVERAGE_INCIDENT = (
    "inventory-service is writing customer names, addresses and payment tokens "
    "into its debug logs after yesterday's log-level change. Nothing is slow and "
    "no stock numbers are wrong — this is a data-handling incident, and we need "
    "to know how to contain and purge the exposed records."
)

#: The positive control: squarely covered by inventory-oversell-incident.
COVERED_INCIDENT = (
    "More units have been sold than we physically have. Available stock has gone "
    "negative and orders were accepted against stock already promised to other "
    "orders."
)


@pytest_asyncio.fixture
async def pipeline() -> AsyncIterator[HybridRetriever]:
    """The full graded pipeline against real infrastructure, or skip."""
    embed_token = SECRETS / "gateway_token_embed"
    reason_token = SECRETS / "gateway_token_reason"
    for path in (embed_token, reason_token):
        if not path.exists():
            pytest.skip(f"no gateway token at {path}")

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

    store = ElasticKnowledgeStore(hosts=ES_URL, dims=1536, index=INDEX)
    try:
        count = await store._client.count(index=INDEX)  # noqa: SLF001
    except Exception as exc:  # pragma: no cover - only when ES is absent
        await store.close()
        await http.aclose()
        pytest.skip(f"no Elasticsearch at {ES_URL}: {exc}")
    if not count["count"]:
        await store.close()
        await http.aclose()
        pytest.skip(f"{INDEX} is empty — build it before running this gate")

    yield HybridRetriever(
        backend=store,
        embedder=GatewayEmbeddingClient(
            http, SecretStr(embed_token.read_text().strip()), dims=1536
        ),
        grader=GatewayGrader(http, SecretStr(reason_token.read_text().strip())),
    )
    await store.close()
    await http.aclose()


def _grading_events(captured: Sequence[Mapping[str, Any]]) -> tuple[bool, bool]:
    """(the grader executed, the grader degraded) — read from the logs."""
    events = [entry.get("event") for entry in captured]
    return "knowledge.graded" in events, "knowledge.grading_skipped" in events


async def test_genuine_no_coverage_yields_an_empty_context_by_judgment(
    pipeline: HybridRetriever,
) -> None:
    """THE gate. Empty context, from a grader that ran, every time."""
    for attempt in range(GATE_REPEATS):
        with structlog.testing.capture_logs() as captured:
            result = await pipeline.retrieve(
                NO_COVERAGE_INCIDENT, service_name="inventory-service", limit=5
            )
        executed, degraded = _grading_events(captured)

        # Empty from JUDGMENT: the grading call ran and said no. A degraded call
        # returns chunks ungraded, so an empty result plus a skip event would
        # mean something is deeply wrong — assert both directions explicitly
        # rather than inferring one from the other.
        assert executed, f"attempt {attempt + 1}: the grading call never ran"
        assert not degraded, f"attempt {attempt + 1}: grading degraded, not judged"
        assert result == [], (
            f"attempt {attempt + 1}: a no-coverage incident came back with "
            f"{[(c['runbook_id'], c.get('grade')) for c in result]} — CRAG could "
            f"not say no, which is the one thing it exists to do"
        )


async def test_a_covered_incident_still_gets_a_graded_context(
    pipeline: HybridRetriever,
) -> None:
    """The other direction: a grader that always says no would pass the gate
    above while destroying retrieval. Only the pair proves discrimination."""
    with structlog.testing.capture_logs() as captured:
        result = await pipeline.retrieve(
            COVERED_INCIDENT, service_name="inventory-service", limit=5
        )
    executed, degraded = _grading_events(captured)

    assert executed and not degraded
    assert result, "a squarely-covered incident must not come back empty"
    assert all(c.get("grade") for c in result), "kept chunks must carry grades"
    assert any(c["runbook_id"] == "inventory-oversell-incident" for c in result), (
        f"expected the oversell runbook, got {[c['runbook_id'] for c in result]}"
    )
