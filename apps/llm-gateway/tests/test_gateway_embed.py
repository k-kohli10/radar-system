"""The /v1/embed endpoint: contract shape and per-input budget semantics."""

from __future__ import annotations

from gateway_harness import GatewayHarness


def test_embed_happy_path_matches_contract_shape(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/embed",
        json={"mode": "embed", "input": ["chunk one", "chunk two"]},
        headers=gw.embed_headers(),
    )
    assert response.status_code == 200
    body = response.json()
    assert set(body) == {"embeddings", "model", "usage"}
    assert len(body["embeddings"]) == 2
    assert body["model"] == "text-embedding-3-small"
    # ~4 chars/token estimate: two 9-char inputs -> 3 + 3
    assert body["usage"] == {"prompt_tokens": 6}


def test_embed_budget_is_per_input_not_per_batch(gw: GatewayHarness) -> None:
    # One oversized input rejects the request...
    response = gw.client.post(
        "/v1/embed",
        json={"mode": "embed", "input": ["ok", "y" * 40000]},
        headers=gw.embed_headers(),
    )
    assert response.status_code == 422
    assert gw.embedder.calls == 0

    # ...but many small inputs whose TOTAL exceeds the per-input limit pass.
    response = gw.client.post(
        "/v1/embed",
        json={"mode": "embed", "input": ["a" * 4000] * 20},
        headers=gw.embed_headers(),
    )
    assert response.status_code == 200
    assert gw.embedder.calls == 1


def test_embed_requires_token_and_mode(gw: GatewayHarness) -> None:
    body = {"mode": "embed", "input": ["x"]}
    assert gw.client.post("/v1/embed", json=body).status_code == 401
    assert (
        gw.client.post("/v1/embed", json=body, headers=gw.fast_headers()).status_code
        == 403
    )


def test_embed_empty_input_rejected(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/embed",
        json={"mode": "embed", "input": []},
        headers=gw.embed_headers(),
    )
    assert response.status_code == 422


def test_embed_provider_failure_returns_503_after_retries(
    gw: GatewayHarness,
) -> None:
    gw.embedder.fail_times = 99
    response = gw.client.post(
        "/v1/embed",
        json={"mode": "embed", "input": ["chunk"]},
        headers=gw.embed_headers(),
    )
    assert response.status_code == 503
    assert gw.embedder.calls == 4  # embed has no fallback configured
    assert gw.sleeps == [1.0, 3.0, 9.0]
