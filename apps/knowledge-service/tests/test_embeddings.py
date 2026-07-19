"""Tests for the gateway embedding client.

Mocked by default (transport-level, so the real httpx request path runs), plus a
``live`` test against the real gateway and real OpenAI — opt-in, because it
spends money.

The properties that matter most are the ones that would corrupt the index
*silently* rather than fail: vectors arriving in a different count than inputs
(chunks would be paired with the wrong vectors, by position), and vectors of a
different dimension (the embedding model changed underneath the index).
"""

from __future__ import annotations

import json
import os
import pathlib
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from radar_common import AGENT_TOKEN_HEADER, ConfigurationError
from radar_knowledge_service.embeddings import (
    DEFAULT_MAX_INPUT_TOKENS,
    EmbeddingContractError,
    EmbeddingRejectedError,
    EmbeddingTimeoutError,
    EmbeddingUnavailableError,
    GatewayEmbeddingClient,
    estimate_input_tokens,
)

DIMS = 8  # small, so fixtures stay readable; dimension logic is dimension-agnostic
TOKEN = SecretStr("t" * 64)


def _client(handler: Any, **kwargs: Any) -> GatewayEmbeddingClient:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(
        transport=transport, base_url="http://gateway", timeout=None
    )
    kwargs.setdefault("dims", DIMS)
    return GatewayEmbeddingClient(http, TOKEN, **kwargs)


def _ok(count: int, *, dims: int = DIMS, model: str = "text-embedding-3-small") -> Any:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "embeddings": [[0.1] * dims for _ in range(count)],
                "model": model,
                "usage": {"prompt_tokens": 10},
            },
        )

    return handler


# ------------------------------------------------------------------- request


async def test_embed_sends_mode_embed_and_the_agent_token() -> None:
    seen: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        seen["token"] = request.headers.get(AGENT_TOKEN_HEADER)
        seen["path"] = request.url.path
        return httpx.Response(
            200, json={"embeddings": [[0.1] * DIMS], "model": "m", "usage": {}}
        )

    await _client(handler).embed(["one"])

    assert seen["path"] == "/v1/embed"
    assert seen["body"] == {"mode": "embed", "input": ["one"]}
    assert seen["token"] == TOKEN.get_secret_value()


async def test_embed_returns_one_vector_per_input_in_order() -> None:
    vectors = await _client(_ok(3)).embed(["a", "b", "c"])

    assert len(vectors) == 3
    assert all(len(v) == DIMS for v in vectors)


async def test_embed_of_nothing_makes_no_call() -> None:
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("the gateway must not be called for an empty list")

    assert await _client(handler).embed([]) == []


async def test_inputs_are_batched_and_reassembled_in_order() -> None:
    """A batched call must still line up one-for-one with its inputs."""
    batches: list[list[str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        batch = json.loads(request.content)["input"]
        batches.append(batch)
        # Encode the input's identity in the vector so misassembly is visible.
        return httpx.Response(
            200,
            json={
                "embeddings": [[float(int(text))] * DIMS for text in batch],
                "model": "m",
                "usage": {},
            },
        )

    texts = [str(i) for i in range(10)]
    vectors = await _client(handler, batch_size=4).embed(texts)

    assert [len(b) for b in batches] == [4, 4, 2]
    assert [v[0] for v in vectors] == [float(i) for i in range(10)]


# -------------------------------------------------------------- budget check


def test_input_token_estimate_is_the_shared_gateway_estimator() -> None:
    assert estimate_input_tokens("") == 0
    assert estimate_input_tokens("abcd") == 1
    assert estimate_input_tokens("abcde") == 2  # ceil, as the gateway does


async def test_an_oversized_input_is_rejected_before_any_call() -> None:
    """The assertion the chunker defers: the client knows the model's limit.

    The gateway would 422 the WHOLE batch; failing locally names the offender.
    """

    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("must not call the gateway with an oversized input")

    oversized = "x" * (DEFAULT_MAX_INPUT_TOKENS * 4 + 4)

    with pytest.raises(EmbeddingRejectedError, match="input 1 is ~"):
        await _client(handler).embed(["fine", oversized])


async def test_the_real_corpus_fits_the_embed_budget() -> None:
    """Every committed runbook chunk is within the per-input limit."""
    from radar_knowledge_service.chunking import chunk_runbook

    root = pathlib.Path(__file__).resolve().parents[3] / "docs" / "runbooks"
    paths = sorted(p for p in root.glob("*.md") if p.name != "README.md")
    assert paths, f"no runbooks under {root} — this test just stopped checking"

    texts = [c.text for p in paths for c in chunk_runbook(p.read_text())]
    assert texts

    # Does not call the gateway: check_budget is a pure local check.
    _client(_ok(1)).check_budget(texts)


# ----------------------------------------------------------------- failures


@pytest.mark.parametrize("status", [401, 403, 422])
async def test_gateway_refusals_raise_rejected(status: int) -> None:
    """Our misconfiguration: it recurs every run and must be loud."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "no"})

    with pytest.raises(EmbeddingRejectedError, match=str(status)):
        await _client(handler).embed(["a"])


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_gateway_failures_raise_unavailable(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json={"detail": "down"})

    with pytest.raises(EmbeddingUnavailableError, match=str(status)):
        await _client(handler).embed(["a"])


async def test_unreachable_gateway_raises_unavailable() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused")

    with pytest.raises(EmbeddingUnavailableError, match="cannot reach"):
        await _client(handler).embed(["a"])


async def test_budget_overrun_raises_timeout() -> None:
    import asyncio

    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(1)
        return httpx.Response(200, json={"embeddings": [[0.1] * DIMS], "model": "m"})

    with pytest.raises(EmbeddingTimeoutError, match="did not return embeddings"):
        await _client(handler, budget_seconds=0.05).embed(["a"])


async def test_a_200_that_is_not_an_embed_response_raises() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    with pytest.raises(EmbeddingContractError, match="not an embed response"):
        await _client(handler).embed(["a"])


# ------------------------------------------------- silent-corruption guards


async def test_a_short_vector_list_raises_rather_than_misaligning() -> None:
    """Chunks pair with vectors BY POSITION.

    Two vectors for three chunks would attach chunk 3's identity to nothing and
    silently mis-index — the index would look fine and answer wrongly.
    """
    with pytest.raises(EmbeddingContractError, match="asked for 3 embeddings"):
        await _client(_ok(2)).embed(["a", "b", "c"])


async def test_a_long_vector_list_raises_too() -> None:
    with pytest.raises(EmbeddingContractError, match="asked for 1 embeddings"):
        await _client(_ok(2)).embed(["a"])


async def test_a_wrong_dimension_raises_and_names_the_consequence() -> None:
    """A silent model swap must not reach the bulk request.

    The index cannot hold a different dimension, so this needs a re-index — the
    error says so rather than leaving it to be discovered per rejected document.
    """
    with pytest.raises(EmbeddingContractError, match="needs a new index"):
        await _client(_ok(1, dims=DIMS + 1)).embed(["a"])


# ------------------------------------------------------------- construction


async def test_a_client_whose_timeout_undercuts_the_budget_is_refused() -> None:
    """Two clocks that disagree means the unreasoned one is in force."""
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(_ok(1)),
        base_url="http://gateway",
        timeout=httpx.Timeout(5.0),
    )

    with pytest.raises(ConfigurationError, match="bound actually in force"):
        GatewayEmbeddingClient(http, TOKEN, dims=DIMS, budget_seconds=30.0)


async def test_a_client_with_no_timeout_is_accepted() -> None:
    http = httpx.AsyncClient(
        transport=httpx.MockTransport(_ok(1)), base_url="http://gw", timeout=None
    )

    GatewayEmbeddingClient(http, TOKEN, dims=DIMS, budget_seconds=30.0)


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({"budget_seconds": 0}, "greater than zero"),
        ({"batch_size": 0}, "at least one"),
    ],
)
async def test_nonsense_configuration_is_refused(kwargs: Any, match: str) -> None:
    with pytest.raises(ConfigurationError, match=match):
        _client(_ok(1), **kwargs)


# ------------------------------------------------------- real gateway (live)


GATEWAY_URL = os.environ.get("RADAR_GATEWAY_URL", "http://127.0.0.1:8098")
TOKEN_FILE = pathlib.Path.home() / ".radar-dev/secrets/knowledge-service/gateway_token"


@pytest.mark.live
async def test_real_gateway_returns_real_1536_dimension_vectors() -> None:
    """End to end on the permanent grant: real gateway, real OpenAI.

    The mocked tests prove the client's logic; only this proves the whole path —
    the knowledge-service token, the embed mode, and the dimension the
    Elasticsearch mapping is built for.
    """
    if not TOKEN_FILE.exists():
        pytest.skip(f"no knowledge-service gateway token at {TOKEN_FILE}")

    http = httpx.AsyncClient(base_url=GATEWAY_URL, timeout=None)
    try:
        await http.get("/healthz")
    except httpx.HTTPError:
        await http.aclose()
        pytest.skip(f"no llm-gateway at {GATEWAY_URL}")

    client = GatewayEmbeddingClient(
        http, SecretStr(TOKEN_FILE.read_text().strip()), dims=1536
    )
    try:
        vectors = await client.embed(
            ["order-service failure rate is elevated", "checkout timeouts"]
        )
    finally:
        await http.aclose()

    assert len(vectors) == 2
    assert all(len(v) == 1536 for v in vectors)
    # Distinct inputs must produce distinct vectors — a constant-vector bug
    # would satisfy every assertion above.
    assert vectors[0] != vectors[1]
