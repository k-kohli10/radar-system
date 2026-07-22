"""The context API: the boundary the reasoner grounds RCAs across.

Two layers:

- **Router with fakes**: the contract — query assembly reaches retrieval, the
  response shape, and above all that EMPTY and UNAVAILABLE stay distinct. The
  pipeline behind the router is proven elsewhere and is not re-proven here.
- **The assembled app**: auth ordering (401 beats 422) and startup behaviour,
  which only exist at app level.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from prometheus_client import CollectorRegistry
from radar_common import AgentTokenAuth
from radar_knowledge_service.api import create_context_router

pytestmark = pytest.mark.asyncio

TOKEN = "k" * 64
AUTH = {"X-Radar-Agent-Token": TOKEN}


class FakeRetriever:
    """Records the call; returns what it is told, or raises what it is told."""

    def __init__(
        self,
        chunks: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.chunks = chunks or []
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def retrieve(
        self, query: str, *, service_name: str | None = None, limit: int = 5
    ) -> list[dict[str, Any]]:
        self.calls.append(
            {"query": query, "service_name": service_name, "limit": limit}
        )
        if self.error is not None:
            raise self.error
        return self.chunks


def _chunk(runbook_id: str, **overrides: Any) -> dict[str, Any]:
    return {
        "chunk_id": "c-" + runbook_id,
        "runbook_id": runbook_id,
        "title": "A Title",
        "section": "Summary",
        "text": f"content for {runbook_id}",
        "score": 0.9,
        **overrides,
    }


def _app(retriever: FakeRetriever) -> FastAPI:
    app = FastAPI()
    app.include_router(
        create_context_router(
            get_retriever=lambda: retriever,
            get_agent_auth=lambda: AgentTokenAuth([TOKEN]),
        )
    )
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://ks"
    )


def _request(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "service_name": "inventory-service",
        "alert_name": "InventoryCheckLatency",
        "investigation_steps": [{"order": 1, "description": "Check cache hit rate"}],
    }
    body.update(overrides)
    return body


# ------------------------------------------------------------------ contract


async def test_the_query_is_assembled_from_the_incident_fields() -> None:
    """The probes measured retrieve(query-string); production must reach the
    same path through build_query, or the evaluation measured a fiction."""
    retriever = FakeRetriever()

    async with _client(_app(retriever)) as client:
        response = await client.post("/v1/context", json=_request(), headers=AUTH)

    assert response.status_code == 200
    (call,) = retriever.calls
    assert call["query"] == (
        "inventory-service InventoryCheckLatency Check cache hit rate"
    )
    assert call["service_name"] == "inventory-service"
    assert call["limit"] == 5


async def test_graded_chunks_come_back_in_the_v2_entry_shape() -> None:
    retriever = FakeRetriever(
        chunks=[_chunk("inventory-check-latency", grade="sufficient")]
    )

    async with _client(_app(retriever)) as client:
        response = await client.post("/v1/context", json=_request(), headers=AUTH)

    assert response.json() == {
        "chunks": [
            {
                "runbook_id": "inventory-check-latency",
                "title": "A Title",
                "section": "Summary",
                "content": "content for inventory-check-latency",
                "grade": "sufficient",
                "status": None,
            }
        ]
    }


async def test_internal_fields_do_not_leak_into_the_response() -> None:
    """chunk_id and the per-leg score are pipeline internals. The score in
    particular is on whichever scale the leg that found the chunk uses, and
    exposing it would invite treating incomparable numbers as comparable."""
    retriever = FakeRetriever(chunks=[_chunk("rb", grade="partial")])

    async with _client(_app(retriever)) as client:
        (entry,) = (
            await client.post("/v1/context", json=_request(), headers=AUTH)
        ).json()["chunks"]

    assert "chunk_id" not in entry
    assert "score" not in entry


async def test_the_fixture_status_is_passed_through() -> None:
    """The corpus is unreviewed fixture content, and the reasoner should know.

    Grounding an RCA in a runbook nobody has reviewed is fine; presenting it as
    though a reviewed runbook backed it is not.
    """
    retriever = FakeRetriever(chunks=[_chunk("rb", status="fixture")])

    async with _client(_app(retriever)) as client:
        (entry,) = (
            await client.post("/v1/context", json=_request(), headers=AUTH)
        ).json()["chunks"]

    assert entry["status"] == "fixture"


async def test_ungraded_chunks_carry_a_null_grade() -> None:
    """A degraded grading call returns ungraded chunks; the reasoner must be
    able to tell vouched-for from unvetted through the API too."""
    retriever = FakeRetriever(chunks=[_chunk("rb")])

    async with _client(_app(retriever)) as client:
        (entry,) = (
            await client.post("/v1/context", json=_request(), headers=AUTH)
        ).json()["chunks"]

    assert entry["grade"] is None


async def test_an_empty_context_is_a_200_answer_not_an_error() -> None:
    """CRAG judging nothing relevant is the stage's most valuable output."""
    async with _client(_app(FakeRetriever(chunks=[]))) as client:
        response = await client.post("/v1/context", json=_request(), headers=AUTH)

    assert response.status_code == 200
    assert response.json() == {"chunks": []}


async def test_steps_are_optional() -> None:
    """Retrieval must work before the planner has run."""
    retriever = FakeRetriever()

    async with _client(_app(retriever)) as client:
        response = await client.post(
            "/v1/context",
            json={"service_name": "svc", "alert_name": "Alert"},
            headers=AUTH,
        )

    assert response.status_code == 200
    assert retriever.calls[0]["query"] == "svc Alert"


# --------------------------------------- unavailable is 503, never an empty 200


async def test_retrieval_failure_is_503_not_an_empty_200() -> None:
    """THE distinction. An empty 200 here would manufacture CRAG's "the corpus
    has nothing for this incident" claim out of an outage, and the stored
    bundle could never again distinguish judged-empty from failed."""
    retriever = FakeRetriever(error=RuntimeError("es exploded: internal detail"))

    async with _client(_app(retriever)) as client:
        response = await client.post("/v1/context", json=_request(), headers=AUTH)

    assert response.status_code == 503
    assert response.json() != {"chunks": []}


async def test_failure_detail_carries_the_class_name_never_the_message() -> None:
    """Vendor messages can carry request detail — same redaction rule as the
    llm-gateway applies to provider errors."""
    retriever = FakeRetriever(
        error=ConnectionError("http://user:password@10.0.0.5:9200 refused")
    )

    async with _client(_app(retriever)) as client:
        response = await client.post("/v1/context", json=_request(), headers=AUTH)

    detail = response.json()["detail"]
    assert "ConnectionError" in detail
    assert "password" not in detail
    assert "10.0.0.5" not in detail


async def test_blank_identifiers_are_422() -> None:
    """A blank service_name would retrieve plausible chunks for the wrong
    service — refused at the boundary, same as build_query refuses it."""
    async with _client(_app(FakeRetriever())) as client:
        response = await client.post(
            "/v1/context", json=_request(service_name="   "), headers=AUTH
        )

    assert response.status_code == 422


async def test_unknown_fields_are_rejected() -> None:
    """extra=forbid: a caller sending a `limit` must hear no, not be ignored."""
    async with _client(_app(FakeRetriever())) as client:
        response = await client.post(
            "/v1/context", json=_request(limit=50), headers=AUTH
        )

    assert response.status_code == 422


# ----------------------------------------------------------------------- auth


async def test_a_missing_token_is_401() -> None:
    async with _client(_app(FakeRetriever())) as client:
        response = await client.post("/v1/context", json=_request())

    assert response.status_code == 401


async def test_a_wrong_token_is_401() -> None:
    async with _client(_app(FakeRetriever())) as client:
        response = await client.post(
            "/v1/context",
            json=_request(),
            headers={"X-Radar-Agent-Token": "x" * 64},
        )

    assert response.status_code == 401


# ------------------------------------------------------------ assembled app


@pytest.fixture
def app_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A secrets directory holding all three tokens the lifespan loads."""
    (tmp_path / "agent_token").write_text(TOKEN)
    (tmp_path / "gateway_token_embed").write_text("e" * 64)
    (tmp_path / "gateway_token_reason").write_text("r" * 64)
    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path))
    return tmp_path


async def test_malformed_json_with_a_bad_token_returns_401_not_422(
    app_secrets: Path,
) -> None:
    """An unauthenticated caller must not learn the contract's shape — the same
    rule the llm-gateway enforces on its guarded paths."""
    from radar_knowledge_service.main import create_app

    app = create_app(metrics_registry=CollectorRegistry(), with_tracing=False)
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://ks"
            ) as client:
                garbage = await client.post(
                    "/v1/context",
                    content=b"{not json",
                    headers={
                        "X-Radar-Agent-Token": "wrong",
                        "Content-Type": "application/json",
                    },
                )
                authenticated = await client.post(
                    "/v1/context",
                    content=b"{not json",
                    headers={**AUTH, "Content-Type": "application/json"},
                )

    assert garbage.status_code == 401
    assert authenticated.status_code == 422


async def test_startup_without_secrets_leaves_readyz_503(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A missing Vault mount is a 503, not a crash no probe ever sees."""
    from radar_knowledge_service.main import create_app

    monkeypatch.setenv("RADAR_SECRETS_DIR", str(tmp_path))  # empty: no tokens
    app = create_app(metrics_registry=CollectorRegistry(), with_tracing=False)
    async with httpx.ASGITransport(app=app) as transport:
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=transport, base_url="http://ks"
            ) as client:
                ready = await client.get("/readyz")
                health = await client.get("/healthz")
                context = await client.post(
                    "/v1/context", json=_request(), headers=AUTH
                )

    assert ready.status_code == 503
    assert health.status_code == 200  # liveness is not readiness
    assert context.status_code == 503  # not 500, and not an empty 200
