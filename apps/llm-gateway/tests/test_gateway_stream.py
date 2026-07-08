"""Streaming: SSE headers, pre-first-event failover, terminal error events."""

from __future__ import annotations

import json
from typing import Any

from gateway_harness import SECRET_PROMPT, SECRET_VENDOR, GatewayHarness, chat_body


def _frames(harness: GatewayHarness, headers: dict[str, str]) -> tuple[Any, list[str]]:
    with harness.client.stream(
        "POST",
        "/v1/complete",
        json=chat_body(mode="extended", stream=True),
        headers=headers,
    ) as response:
        lines = [line for line in response.iter_lines() if line.startswith("data: ")]
        return response, lines


def test_sse_headers_set_before_body(gw: GatewayHarness) -> None:
    response, frames = _frames(gw, gw.extended_headers())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache"
    # nginx ingress buffers by default and would break SSE without this.
    assert response.headers["x-accel-buffering"] == "no"
    assert len(frames) == 3  # two deltas + done


def test_stream_happy_path_ends_with_done_and_usage(gw: GatewayHarness) -> None:
    _, frames = _frames(gw, gw.extended_headers())
    deltas = [json.loads(f.removeprefix("data: ")) for f in frames]
    assert [d["delta"] for d in deltas[:-1]] == ["hello ", "world"]
    terminal = deltas[-1]
    assert terminal["done"] is True
    assert terminal["usage"] == {"prompt_tokens": 7, "completion_tokens": 3}


def test_pre_first_event_failure_fails_over_to_a_clean_stream(
    gw: GatewayHarness,
) -> None:
    # Primary dies before its first event on all four attempts; the fallback
    # serves the stream and the client never sees any error artifact.
    gw.primary_chat.stream_scripts = [["X"], ["X"], ["X"], ["X"]]
    response, frames = _frames(gw, gw.extended_headers())
    assert response.status_code == 200
    body = "".join(frames)
    assert "stream_failed" not in body
    assert json.loads(frames[-1].removeprefix("data: "))["done"] is True
    assert gw.primary_chat.calls == 4
    assert gw.fallback_chat.calls == 1
    assert gw.sleeps == [1.0, 3.0, 9.0]


def test_pre_stream_total_failure_returns_json_503(gw: GatewayHarness) -> None:
    gw.primary_chat.stream_scripts = [["X"]] * 4
    gw.fallback_chat.stream_scripts = [["X"]] * 4
    response = gw.client.post(
        "/v1/complete",
        json=chat_body(mode="extended", stream=True),
        headers=gw.extended_headers(),
    )
    assert response.status_code == 503
    assert response.headers["content-type"].startswith("application/json")


def test_mid_stream_failure_emits_terminal_error_event(gw: GatewayHarness) -> None:
    """A dying stream must never close silently: the last frame is the
    documented error event with provider and recoverable flag only."""
    gw.primary_chat.stream_scripts = [["partial ", "X"]]
    response, frames = _frames(gw, gw.extended_headers())
    assert response.status_code == 200  # headers were already sent
    assert json.loads(frames[0].removeprefix("data: "))["delta"] == "partial "
    terminal = json.loads(frames[-1].removeprefix("data: "))
    assert terminal == {
        "error": "stream_failed",
        "provider": "openai",
        "recoverable": True,
    }
    assert (
        gw.metric(
            "radar_llm_requests_total",
            mode="extended",
            provider="openai",
            status="stream_failed",
        )
        == 1
    )


def test_mid_stream_non_retryable_failure_is_not_recoverable(
    gw: GatewayHarness,
) -> None:
    gw.primary_chat.fail_status = 400
    gw.primary_chat.stream_scripts = [["partial ", "X"]]
    _, frames = _frames(gw, gw.extended_headers())
    terminal = json.loads(frames[-1].removeprefix("data: "))
    assert terminal["recoverable"] is False


def test_stream_frames_never_contain_content_or_vendor_text(
    gw: GatewayHarness,
) -> None:
    gw.primary_chat.stream_scripts = [["partial ", "X"]]
    _, frames = _frames(gw, gw.extended_headers())
    body = "".join(frames)
    assert SECRET_PROMPT not in body  # the request prompt
    assert SECRET_VENDOR not in body  # the vendor exception message
