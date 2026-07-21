"""The CRAG grading call.

The decisions are proven in ``test_crag.py``. These prove the CALL, and above all
the distinction the stage depends on: an EMPTY result means the model judged
nothing relevant, while a FAILED call returns the chunks ungraded. Collapsing
those two would make the stage's one meaningful output — "the corpus has nothing
for this incident" — indistinguishable from a network error.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from radar_common import AGENT_TOKEN_HEADER, ConfigurationError
from radar_knowledge_service.crag_client import GatewayGrader

pytestmark = pytest.mark.asyncio

TOKEN = SecretStr("c" * 64)


def _c(chunk_id: str) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "runbook_id": "rb",
        "section": "Summary",
        "text": f"text for {chunk_id}",
    }


def _client(handler: Any) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://gateway", timeout=None
    )


def _reply(content: str) -> httpx.Response:
    return httpx.Response(200, json={"content": content})


def _grades(**pairs: str) -> str:
    entries = ", ".join(
        f'{{"chunk_id": "{k}", "grade": "{v}"}}' for k, v in pairs.items()
    )
    return f'{{"grades": [{entries}]}}'


async def test_all_insufficient_returns_an_empty_context() -> None:
    """The stage's reason for existing, through the real call path."""
    async with _client(
        lambda _: _reply(_grades(a="insufficient", b="insufficient"))
    ) as http:
        result = await GatewayGrader(http, TOKEN).grade("q", [_c("a"), _c("b")])

    assert result == []


async def test_usable_chunks_come_back_carrying_their_grades() -> None:
    async with _client(
        lambda _: _reply(_grades(a="sufficient", b="insufficient"))
    ) as http:
        result = await GatewayGrader(http, TOKEN).grade("q", [_c("a"), _c("b")])

    assert [c["chunk_id"] for c in result] == ["a"]
    assert result[0]["grade"] == "sufficient"


async def test_every_chunk_is_graded_in_one_request() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _reply(_grades(a="partial", b="partial", c="partial", d="partial"))

    async with _client(handler) as http:
        await GatewayGrader(http, TOKEN).grade(
            "q", [_c("a"), _c("b"), _c("c"), _c("d")]
        )

    assert len(requests) == 1
    body = requests[0].content.decode()
    for chunk_id in ("a", "b", "c", "d"):
        assert f"chunk_id: {chunk_id}" in body


async def test_the_call_uses_reason_mode_and_the_grading_token() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return _reply(_grades(a="partial"))

    async with _client(handler) as http:
        await GatewayGrader(http, TOKEN).grade("q", [_c("a")])

    body = json.loads(requests[0].content)
    assert body["mode"] == "reason"
    assert requests[0].headers[AGENT_TOKEN_HEADER] == TOKEN.get_secret_value()


async def test_no_chunks_makes_no_call() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return _reply(_grades())

    async with _client(handler) as http:
        assert await GatewayGrader(http, TOKEN).grade("q", []) == []

    assert calls == []


# ------------------------------------- failure must NOT look like "nothing fits"


@pytest.mark.parametrize("status", [401, 403, 422, 500, 503])
async def test_an_error_status_returns_the_chunks_ungraded_not_empty(
    status: int,
) -> None:
    """THE distinction the stage depends on.

    Returning empty here would manufacture the claim "the corpus has nothing for
    this incident" out of a network failure, and make the one output that should
    be trusted untrustworthy.
    """
    chunks = [_c("a"), _c("b")]

    async with _client(lambda _: httpx.Response(status, json={})) as http:
        result = await GatewayGrader(http, TOKEN).grade("q", chunks)

    assert [c["chunk_id"] for c in result] == ["a", "b"]
    assert all("grade" not in c for c in result), (
        "ungraded chunks must not claim a grade"
    )


async def test_a_transport_failure_returns_the_chunks_ungraded() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("gateway is down")

    async with _client(handler) as http:
        result = await GatewayGrader(http, TOKEN).grade("q", [_c("a")])

    assert [c["chunk_id"] for c in result] == ["a"]


async def test_an_incomplete_reply_returns_the_chunks_ungraded() -> None:
    """Two chunks sent, one graded: unusable, so nothing is claimed about either."""
    async with _client(lambda _: _reply(_grades(a="sufficient"))) as http:
        result = await GatewayGrader(http, TOKEN).grade("q", [_c("a"), _c("b")])

    assert [c["chunk_id"] for c in result] == ["a", "b"]
    assert all("grade" not in c for c in result)


async def test_a_prose_reply_returns_the_chunks_ungraded() -> None:
    async with _client(lambda _: _reply("They all look relevant to me!")) as http:
        result = await GatewayGrader(http, TOKEN).grade("q", [_c("a")])

    assert [c["chunk_id"] for c in result] == ["a"]


# ------------------------------------------------------------------- clocks


async def test_a_client_timeout_shorter_than_the_budget_is_refused() -> None:
    async with httpx.AsyncClient(timeout=5.0) as http:
        with pytest.raises(ConfigurationError, match="shorter than"):
            GatewayGrader(http, TOKEN, budget_seconds=30.0)


async def test_a_non_positive_budget_is_refused() -> None:
    async with httpx.AsyncClient(timeout=None) as http:
        with pytest.raises(ConfigurationError, match="greater than zero"):
            GatewayGrader(http, TOKEN, budget_seconds=0)
