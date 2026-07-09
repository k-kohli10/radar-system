"""The logging policy: prompt content is absent from ALL log output.

Every failure and success path is exercised with sentinel strings planted in
the message content (:data:`SECRET_PROMPT`) and in every vendor exception
(:data:`SECRET_VENDOR`); the entire captured log output must contain neither.
"""

from __future__ import annotations

import io
import json
import logging
from collections.abc import Iterator

import pytest
from gateway_harness import SECRET_PROMPT, SECRET_VENDOR, GatewayHarness, chat_body


@pytest.fixture
def captured_logs() -> Iterator[io.StringIO]:
    """Capture everything that reaches the stdlib root logger."""
    buffer = io.StringIO()
    handler = logging.StreamHandler(buffer)
    root = logging.getLogger()
    previous_level = root.level
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield buffer
    finally:
        root.removeHandler(handler)
        root.setLevel(previous_level)


def test_prompt_content_absent_from_all_log_output(
    gw: GatewayHarness,
    captured_logs: io.StringIO,
    capfd: pytest.CaptureFixture[str],
) -> None:
    # success path (logs llm.request)
    ok = gw.client.post("/v1/complete", json=chat_body(), headers=gw.fast_headers())
    assert ok.status_code == 200

    # retries + fallback (logs llm.provider_retry and llm.provider_fallback)
    gw.primary_chat.fail_times = 99
    fell_back = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert fell_back.status_code == 200

    # total failure (logs llm.request with 503)
    gw.primary_chat.fail_times = 99
    gw.fallback_chat.fail_times = 99
    dead = gw.client.post(
        "/v1/complete", json=chat_body(mode="extended"), headers=gw.extended_headers()
    )
    assert dead.status_code == 503

    # mid-stream failure (logs llm.stream_failed)
    gw.primary_chat.fail_times = 0
    gw.fallback_chat.fail_times = 0
    gw.primary_chat.stream_scripts = [["partial ", "X"]]
    with gw.client.stream(
        "POST",
        "/v1/complete",
        json=chat_body(mode="extended", stream=True),
        headers=gw.extended_headers(),
    ) as response:
        list(response.iter_lines())

    # over-limit rejection (must not echo content anywhere)
    over = gw.client.post(
        "/v1/complete",
        json=chat_body(content=SECRET_PROMPT + "x" * 20000),
        headers=gw.fast_headers(),
    )
    assert over.status_code == 422

    # Structlog prints to stdout; stdlib-propagated records hit the root
    # logger. Scan both so nothing escapes the assertion.
    stdout_err = capfd.readouterr()
    output = captured_logs.getvalue() + stdout_err.out + stdout_err.err
    assert "llm.request" in output, "expected llm.request lines to be captured"
    assert "llm.provider_retry" in output
    assert "llm.provider_fallback" in output
    assert "llm.stream_failed" in output
    assert SECRET_PROMPT not in output
    assert SECRET_VENDOR not in output


def test_request_log_line_carries_only_allowed_fields(
    gw: GatewayHarness, capfd: pytest.CaptureFixture[str]
) -> None:
    response = gw.client.post(
        "/v1/complete", json=chat_body(), headers=gw.fast_headers()
    )
    assert response.status_code == 200

    request_lines = [
        line for line in capfd.readouterr().out.splitlines() if "llm.request" in line
    ]
    assert request_lines, "expected an llm.request log line"
    line = request_lines[-1]
    for field in (
        "mode",
        "provider",
        "model",
        "prompt_tokens",
        "completion_tokens",
        "latency_ms",
        "status_code",
    ):
        assert field in line
    assert "messages" not in line
    assert "content" not in line


def test_api_key_and_agent_tokens_never_logged(
    gw: GatewayHarness,
    captured_logs: io.StringIO,
    capfd: pytest.CaptureFixture[str],
) -> None:
    gw.client.post("/v1/complete", json=chat_body(), headers=gw.fast_headers())
    gw.client.post(
        "/v1/embed",
        json={"mode": "embed", "input": ["chunk"]},
        headers=gw.embed_headers(),
    )
    stdout_err = capfd.readouterr()
    output = captured_logs.getvalue() + stdout_err.out + stdout_err.err
    assert gw.env.fast_token not in output
    assert gw.env.embed_token not in output
    assert "sk-test-not-real" not in output


def test_metrics_output_contains_no_content_or_secrets(gw: GatewayHarness) -> None:
    gw.client.post("/v1/complete", json=chat_body(), headers=gw.fast_headers())
    from prometheus_client import generate_latest

    scrape = generate_latest(gw.metrics_registry).decode()
    assert SECRET_PROMPT not in scrape
    assert gw.env.fast_token not in scrape


def test_stream_terminal_error_event_shape_is_exactly_the_contract(
    gw: GatewayHarness,
) -> None:
    gw.primary_chat.stream_scripts = [["partial ", "X"]]
    with gw.client.stream(
        "POST",
        "/v1/complete",
        json=chat_body(mode="extended", stream=True),
        headers=gw.extended_headers(),
    ) as response:
        frames = [line for line in response.iter_lines() if line.startswith("data: ")]
    terminal = json.loads(frames[-1].removeprefix("data: "))
    assert set(terminal) == {"error", "provider", "recoverable"}
