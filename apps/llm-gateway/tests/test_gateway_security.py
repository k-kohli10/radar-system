"""Steps 1-5 of the request validation order, enforced over the wired app."""

from __future__ import annotations

import secrets

from gateway_harness import SECRET_PROMPT, GatewayHarness, chat_body
from radar_contracts import LLMMode
from radar_llm_gateway.core.config import TokenGrant, TokenMap


def test_missing_token_returns_401(gw: GatewayHarness) -> None:
    response = gw.client.post("/v1/complete", json=chat_body())
    assert response.status_code == 401


def test_wrong_token_returns_401(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/complete",
        json=chat_body(),
        headers={"X-Radar-Agent-Token": secrets.token_hex(32)},
    )
    assert response.status_code == 401
    assert gw.primary_chat.calls == 0


def test_correct_token_wrong_mode_returns_403(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/complete", json=chat_body(mode="reason"), headers=gw.fast_headers()
    )
    assert response.status_code == 403
    assert gw.primary_chat.calls == 0


def test_over_token_limit_returns_422(gw: GatewayHarness) -> None:
    # fast allows 4096 tokens ~= 16384 chars; send well past it.
    response = gw.client.post(
        "/v1/complete",
        json=chat_body(content="x" * 20000),
        headers=gw.fast_headers(),
    )
    assert response.status_code == 422
    assert gw.primary_chat.calls == 0


def test_malformed_json_with_bad_token_returns_401_not_422(
    gw: GatewayHarness,
) -> None:
    """Pinned security property: FastAPI parses the body before dependencies
    run, so without the guarded validation handler a malformed body would
    422 before auth ever saw the bad token. 401 must win."""
    response = gw.client.post(
        "/v1/complete",
        content=b"{this is not json",
        headers={
            "X-Radar-Agent-Token": "not-a-real-token",
            "content-type": "application/json",
        },
    )
    assert response.status_code == 401

    # And with the header missing entirely.
    response = gw.client.post(
        "/v1/complete",
        content=b"{this is not json",
        headers={"content-type": "application/json"},
    )
    assert response.status_code == 401


def test_malformed_json_with_valid_token_returns_422(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/complete",
        content=b"{this is not json",
        headers={**gw.fast_headers(), "content-type": "application/json"},
    )
    assert response.status_code == 422


def test_schema_invalid_body_with_bad_token_returns_401(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/complete",
        json={"mode": "fast"},  # messages missing
        headers={"X-Radar-Agent-Token": "wrong"},
    )
    assert response.status_code == 401


def test_embed_mode_on_complete_endpoint_returns_422(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/complete", json=chat_body(mode="embed"), headers=gw.embed_headers()
    )
    assert response.status_code == 422


def test_chat_mode_on_embed_endpoint_returns_422(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/embed",
        json={"mode": "fast", "input": ["x"]},
        headers=gw.fast_headers(),
    )
    assert response.status_code == 422


def test_error_details_never_contain_message_content(gw: GatewayHarness) -> None:
    over_limit = gw.client.post(
        "/v1/complete",
        json=chat_body(content=SECRET_PROMPT + "x" * 20000),
        headers=gw.fast_headers(),
    )
    assert over_limit.status_code == 422
    assert SECRET_PROMPT not in over_limit.text

    wrong_mode = gw.client.post(
        "/v1/complete", json=chat_body(mode="reason"), headers=gw.fast_headers()
    )
    assert SECRET_PROMPT not in wrong_mode.text


def test_token_map_repr_never_contains_token_values() -> None:
    token = secrets.token_hex(32)
    token_map = TokenMap(
        {token: TokenGrant(service="watcher-agent", allowed_mode=LLMMode.FAST)}
    )
    assert token not in repr(token_map)
    assert token not in str(token_map)
    assert "watcher-agent" in repr(token_map)


def test_success_still_flows_after_auth(gw: GatewayHarness) -> None:
    response = gw.client.post(
        "/v1/complete", json=chat_body(), headers=gw.fast_headers()
    )
    assert response.status_code == 200
    assert response.json()["content"] == "answer from gpt-4o"
