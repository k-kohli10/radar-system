"""The LLM call: which clock is actually enforcing the bound, and what comes back.

THE QUESTION THIS FILE EXISTS TO ANSWER

There are two timeouts in reach, and only one may be in charge:

- **httpx's own**, which defaults to **5 seconds**
- **the reasoner's budget** (60s), an ``asyncio.timeout`` around the whole call,
  ordered against the outbox worker's 90s dispatch timeout in ``radar_common.timeouts``

If httpx's is the shorter, IT fires — and every number in ``radar_common.timeouts``
becomes decoration, because a third clock nobody was reasoning about is the real bound.
An extended-mode call routinely exceeds 5 seconds, so a client on the default would kill
every call, every incident would fall back to a template RCA, and the service would look
perfectly healthy while never once using the model it exists to use. **There would be no
error to find.** That is the worst kind of bug this codebase can produce, and it was
sitting in the reasoner's lifespan until this commit.

So it is made impossible rather than documented: ``GatewayClient`` REFUSES TO BE
CONSTRUCTED with a client whose timeout could fire before the budget. The test below
constructs one with httpx's default and requires a ConfigurationError — that is the
assertion that identifies which clock enforces, because it makes the wrong one
unconstructible.

ON NOT BURNING 60 SECONDS IN CI

The timeout test drives a gateway that never answers, with the budget COMPRESSED to
0.3s, and asserts the call returns at ~0.3s — proving the budget is the bound and not
some other number. A separate assertion pins the production default AT 60s
(REASONER_LLM_BUDGET_SECONDS), so the shipped value is verified without the suite
sitting there for a minute. Both halves are needed: the first proves the mechanism, the
second proves the number.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr
from radar_common import (
    AGENT_TOKEN_HEADER,
    REASONER_LLM_BUDGET_SECONDS,
    ConfigurationError,
)
from radar_contracts import LLMMode, PlanStep, Severity
from radar_reasoner_agent.context import ContextBundle
from radar_reasoner_agent.llm import (
    SYSTEM_PROMPT,
    GatewayClient,
    LLMFailure,
    LLMFailureReason,
    LLMSuccess,
)

TOKEN = SecretStr("g" * 64)

#: The compressed budget the timing tests run against. Small enough to keep the suite
#: fast, large enough that a passing result cannot be an accident of scheduling.
TEST_BUDGET = 0.3


def _bundle() -> ContextBundle:
    return ContextBundle(
        incident_id=uuid4(),
        service_name="order-service",
        alert_name="OrderProcessingFailureRate",
        severity=Severity.CRITICAL,
        opened_at=datetime(2026, 7, 14, 10, 0, tzinfo=UTC),
        alert_count=3,
        investigation_steps=[PlanStep(order=1, description="Check recent deployments")],
    )


class _Hangs(httpx.AsyncBaseTransport):
    """A gateway that accepts the connection and then never answers."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")


class _Responds(httpx.AsyncBaseTransport):
    """A gateway that returns a canned response, recording what it was asked."""

    def __init__(self, status_code: int, body: Any = None) -> None:
        self._status = status_code
        self._body = body
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self._body is None:
            return httpx.Response(self._status)
        if isinstance(self._body, bytes):
            return httpx.Response(self._status, content=self._body)
        return httpx.Response(self._status, json=self._body)


def _client(transport: httpx.AsyncBaseTransport) -> httpx.AsyncClient:
    """A client configured the way the reasoner's lifespan configures it.

    ``timeout=None``: httpx enforces nothing, so ``asyncio.timeout`` is the only clock.
    """
    return httpx.AsyncClient(transport=transport, base_url="http://gw", timeout=None)


def _ok_response(content: str = '{"root_cause": "x"}') -> dict[str, Any]:
    return {
        "id": "resp_1",
        "mode": "extended",
        "provider": "openai",
        "model": "gpt-4o",
        "content": content,
        "usage": {"prompt_tokens": 100, "completion_tokens": 40},
        "latency_ms": 1234,
    }


# ==========================================================================
# WHICH CLOCK ENFORCES THE BOUND
# ==========================================================================


def test_a_client_whose_timeout_undercuts_the_budget_is_refused() -> None:
    """THE test. httpx's 5-second default must not be able to become the real bound.

    This is the assertion that identifies which clock enforces, and it does it by making
    the wrong one unconstructible. Without it, the reasoner's lifespan could build
    ``httpx.AsyncClient(base_url=...)`` — the default — and every extended-mode call
    would die at 5s: 100% fallback rate, no errors, no crash, and an LLM that is never
    once used. It was exactly that until this commit.
    """
    default_client = httpx.AsyncClient(base_url="http://gw")  # httpx default: 5s
    assert default_client.timeout.read == 5.0, "httpx's default, for the record"

    with pytest.raises(ConfigurationError, match="shorter than the LLM budget"):
        GatewayClient(default_client, TOKEN, budget_seconds=60.0)


def test_a_client_with_no_timeout_is_accepted() -> None:
    """``timeout=None`` — the intended configuration. asyncio owns the clock."""
    client = httpx.AsyncClient(base_url="http://gw", timeout=None)

    gateway = GatewayClient(client, TOKEN, budget_seconds=60.0)  # no raise

    assert gateway is not None


def test_a_client_whose_timeout_exceeds_the_budget_is_accepted() -> None:
    """A longer httpx timeout cannot fire first, so it cannot become the bound."""
    client = httpx.AsyncClient(base_url="http://gw", timeout=120.0)

    GatewayClient(client, TOKEN, budget_seconds=60.0)  # no raise


def test_the_production_budget_is_sixty_seconds() -> None:
    """Pins the shipped number without the suite sitting there for a minute.

    The timing test below proves the MECHANISM (the budget is the bound) with a
    compressed value; this proves the NUMBER. Both are needed — a mechanism verified at
    0.3s tells you nothing about what ships, and a number nobody exercises tells you
    nothing about whether it is enforced.
    """
    gateway = GatewayClient(_client(_Hangs()), TOKEN)  # no budget passed: the default

    assert gateway._budget == REASONER_LLM_BUDGET_SECONDS == 60.0


# ==========================================================================
# THE HANGING GATEWAY
# ==========================================================================


async def test_a_hanging_gateway_times_out_at_the_budget() -> None:
    """Point it at a gateway that never answers. It must return at the BUDGET.

    Not at httpx's default (which is disabled), not never. The elapsed time is asserted
    against the budget on both sides: at least it, and not wildly past it — a call that
    returned instantly would mean the timeout fired for some other reason, and one that
    ran long would mean something else was in charge.
    """
    gateway = GatewayClient(_client(_Hangs()), TOKEN, budget_seconds=TEST_BUDGET)

    started = time.perf_counter()
    # The outer guard: if the budget is NOT enforcing, this fails loudly at 5s instead
    # of hanging the suite for an hour.
    result = await asyncio.wait_for(gateway.complete(_bundle()), timeout=5.0)
    elapsed = time.perf_counter() - started

    assert isinstance(result, LLMFailure)
    assert result.reason is LLMFailureReason.TIMEOUT
    assert f"{TEST_BUDGET:g}s" in result.detail, "the error names the budget in force"

    assert elapsed >= TEST_BUDGET, "it gave up BEFORE the budget — a shorter clock won"
    assert elapsed < TEST_BUDGET * 4, (
        "it ran well past the budget — the asyncio.timeout is not the bound in force"
    )
    # And the failure carries how long we actually waited, for the fallback's context.
    assert result.elapsed_ms >= int(TEST_BUDGET * 1000 * 0.9)


# ==========================================================================
# WHAT COMES BACK
# ==========================================================================


async def test_a_successful_call_returns_the_completion_and_its_provenance() -> None:
    transport = _Responds(200, _ok_response())
    gateway = GatewayClient(_client(transport), TOKEN, budget_seconds=TEST_BUDGET)

    result = await gateway.complete(_bundle())

    assert isinstance(result, LLMSuccess)
    assert result.content == '{"root_cause": "x"}'
    assert result.provider == "openai"
    assert result.model == "gpt-4o"
    assert result.mode == "extended"
    assert result.prompt_tokens == 100
    assert result.completion_tokens == 40
    # The GATEWAY's measured latency, not ours — ours includes our hop to the gateway.
    assert result.latency_ms == 1234


async def test_the_request_asks_for_extended_mode_with_the_system_prompt() -> None:
    """extended, the v1 system prompt, and the bundle as the user message."""
    transport = _Responds(200, _ok_response())
    gateway = GatewayClient(_client(transport), TOKEN, budget_seconds=TEST_BUDGET)
    bundle = _bundle()

    await gateway.complete(bundle)

    import json

    sent = json.loads(transport.requests[0].content)
    assert sent["mode"] == LLMMode.EXTENDED.value, "the mode our token is granted"
    assert sent["messages"][0]["role"] == "system"
    assert sent["messages"][0]["content"] == SYSTEM_PROMPT
    assert sent["messages"][1]["role"] == "user"
    # The model is shown the bundle, and therefore the LIVE severity.
    assert "critical" in sent["messages"][1]["content"]
    assert "OrderProcessingFailureRate" in sent["messages"][1]["content"]


async def test_the_outbound_gateway_token_is_sent() -> None:
    transport = _Responds(200, _ok_response())
    gateway = GatewayClient(_client(transport), TOKEN, budget_seconds=TEST_BUDGET)

    await gateway.complete(_bundle())

    assert transport.requests[0].headers[AGENT_TOKEN_HEADER] == TOKEN.get_secret_value()


# ==========================================================================
# FAILURES, KEPT APART
# ==========================================================================


@pytest.mark.parametrize(
    ("status_code", "reason"),
    [
        pytest.param(503, LLMFailureReason.GATEWAY_UNAVAILABLE, id="503-all-providers"),
        pytest.param(500, LLMFailureReason.GATEWAY_UNAVAILABLE, id="500-gateway-error"),
        pytest.param(429, LLMFailureReason.GATEWAY_UNAVAILABLE, id="429-rate-limited"),
        pytest.param(401, LLMFailureReason.REJECTED, id="401-bad-token"),
        pytest.param(403, LLMFailureReason.REJECTED, id="403-mode-not-granted"),
        pytest.param(422, LLMFailureReason.REJECTED, id="422-request-refused"),
    ],
)
async def test_gateway_failures_are_classified_by_whose_problem_they_are(
    status_code: int, reason: LLMFailureReason
) -> None:
    """Three different operational problems, and the reason is the only thing that
    tells them apart once the fallback has flattened them all into one RCA.

    ``GATEWAY_UNAVAILABLE`` is not a RADAR bug — the LLM is down, the fallback covers
    it. ``REJECTED`` IS a RADAR bug: our token or our mode is wrong, it will fail
    identically on every incident, and a 100% fallback rate with this reason means the
    reasoner has never once used the LLM.
    """
    gateway = GatewayClient(
        _client(_Responds(status_code)), TOKEN, budget_seconds=TEST_BUDGET
    )

    result = await gateway.complete(_bundle())

    assert isinstance(result, LLMFailure)
    assert result.reason is reason


async def test_an_unreachable_gateway_is_not_a_crash() -> None:
    """A connection error is a failure result, never an exception.

    The caller must be able to fall back. An exception escaping here would take the
    handler with it and leave the incident with no RCA at all — which is the one
    outcome this service exists to prevent.
    """

    class _Refuses(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    gateway = GatewayClient(_client(_Refuses()), TOKEN, budget_seconds=TEST_BUDGET)

    result = await gateway.complete(_bundle())

    assert isinstance(result, LLMFailure)
    assert result.reason is LLMFailureReason.GATEWAY_UNAVAILABLE
    assert "ConnectError" in result.detail


async def test_a_200_that_is_not_the_gateways_contract_is_a_failure() -> None:
    """200 with a body that is not an LLMResponse. Not the model's fault, not usable."""
    gateway = GatewayClient(
        _client(_Responds(200, {"nonsense": True})), TOKEN, budget_seconds=TEST_BUDGET
    )

    result = await gateway.complete(_bundle())

    assert isinstance(result, LLMFailure)
    assert result.reason is LLMFailureReason.GATEWAY_UNAVAILABLE
