"""The rerank gateway call.

The decisions are proven in ``test_reranking.py``. These prove the CALL: that it
asks for reason mode with the right token, that it is one request rather than
one per candidate, and — mostly — that every failure path degrades to the fused
ordering instead of raising.

The degradation cases carry the weight. Reranking failing is invisible in the
result by design, so a bug that turns a failure into an exception, or into an
empty list, would surface as incidents losing their context for a reason nobody
could see.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from radar_common import AGENT_TOKEN_HEADER, ConfigurationError
from radar_knowledge_service.rerank_client import GatewayReranker

pytestmark = pytest.mark.asyncio

TOKEN = SecretStr("r" * 64)


def _c(chunk_id: str) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "runbook_id": "rb",
        "section": "Summary",
        "text": f"text for {chunk_id}",
    }


def _client(handler: Any) -> httpx.AsyncClient:
    """A client whose transport is scripted, with no timeout of its own."""
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://gateway",
        timeout=None,
    )


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"content": content})


async def test_scores_reorder_the_candidates() -> None:
    scored = (
        '{"scores": [{"chunk_id": "a", "score": 2}, {"chunk_id": "b", "score": 9}]}'
    )

    async with _client(lambda _: _reply(scored)) as http:
        result = await GatewayReranker(http, TOKEN).rerank("q", [_c("a"), _c("b")])

    assert [c["chunk_id"] for c in result] == ["b", "a"]


async def test_every_candidate_is_scored_in_one_request() -> None:
    """Batched by construction: N candidates must not become N calls."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _reply('{"scores": []}')

    async with _client(handler) as http:
        await GatewayReranker(http, TOKEN).rerank(
            "q", [_c("a"), _c("b"), _c("c"), _c("d")]
        )

    assert len(requests) == 1
    body = requests[0].content.decode()
    for chunk_id in ("a", "b", "c", "d"):
        assert f"chunk_id: {chunk_id}" in body


async def test_the_call_uses_reason_mode_and_the_rerank_token() -> None:
    """Reason mode is what the second gateway token grants.

    Asking in another mode would be refused by the gateway with 403 — and this
    stage degrades silently, so it would present as reranking never working.
    """
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _reply('{"scores": []}')

    async with _client(handler) as http:
        await GatewayReranker(http, TOKEN).rerank("q", [_c("a")])

    # Parsed rather than string-matched: httpx serialises compactly, so a
    # substring check would depend on its separator style rather than on the
    # field's value.
    body = json.loads(requests[0].content)
    assert body["mode"] == "reason"
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert requests[0].headers[AGENT_TOKEN_HEADER] == TOKEN.get_secret_value()


async def test_no_candidates_makes_no_call() -> None:
    """Spending a reason-mode call to rank nothing."""
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _reply('{"scores": []}')

    async with _client(handler) as http:
        assert await GatewayReranker(http, TOKEN).rerank("q", []) == []

    assert calls == []


# ------------------------------------------------------------- degradation


@pytest.mark.parametrize("status", [401, 403, 422, 500, 503])
async def test_an_error_status_degrades_to_the_fused_order(status: int) -> None:
    """Including 401/403: a misconfigured token must not cost the incident its
    context, it must cost it only the improvement."""
    candidates = [_c("a"), _c("b")]

    async with _client(lambda _: httpx.Response(status, json={})) as http:
        result = await GatewayReranker(http, TOKEN).rerank("q", candidates)

    assert [c["chunk_id"] for c in result] == ["a", "b"]


async def test_a_transport_failure_degrades_to_the_fused_order() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway is down")

    async with _client(handler) as http:
        result = await GatewayReranker(http, TOKEN).rerank("q", [_c("a"), _c("b")])

    assert [c["chunk_id"] for c in result] == ["a", "b"]


async def test_an_unparseable_reply_degrades_to_the_fused_order() -> None:
    """The model answering 200 with prose is a normal LLM failure mode."""
    async with _client(lambda _: _reply("I think chunk a is best!")) as http:
        result = await GatewayReranker(http, TOKEN).rerank("q", [_c("a"), _c("b")])

    assert [c["chunk_id"] for c in result] == ["a", "b"]


async def test_a_response_missing_content_degrades_to_the_fused_order() -> None:
    async with _client(lambda _: httpx.Response(200, json={"wrong": "shape"})) as http:
        result = await GatewayReranker(http, TOKEN).rerank("q", [_c("a")])

    assert [c["chunk_id"] for c in result] == ["a"]


async def test_degrading_still_honours_the_limit() -> None:
    """A failure must not return more context than the caller asked for."""
    candidates = [_c("a"), _c("b"), _c("c")]

    async with _client(lambda _: httpx.Response(500, json={})) as http:
        result = await GatewayReranker(http, TOKEN).rerank("q", candidates, limit=2)

    assert [c["chunk_id"] for c in result] == ["a", "b"]


# ------------------------------------------------------------------- clocks


async def test_a_client_timeout_shorter_than_the_budget_is_refused() -> None:
    """Two clocks that disagree means the one nobody reasoned about wins.

    httpx's 5s default would abort every reason-mode call, and because this
    stage degrades silently that would present as reranking simply never doing
    anything.
    """
    async with httpx.AsyncClient(timeout=5.0) as http:
        with pytest.raises(ConfigurationError, match="shorter than"):
            GatewayReranker(http, TOKEN, budget_seconds=30.0)


async def test_a_non_positive_budget_is_refused() -> None:
    async with httpx.AsyncClient(timeout=None) as http:
        with pytest.raises(ConfigurationError, match="greater than zero"):
            GatewayReranker(http, TOKEN, budget_seconds=0)
