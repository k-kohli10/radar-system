"""The LLM call: one request to the gateway, one hard budget, no exceptions escape.

The reasoner asks the gateway for a root-cause analysis in ``extended`` mode. What
comes back is a *result*, never an exception: either :class:`LLMSuccess` or a typed
:class:`LLMFailure` saying WHY it failed. The caller (the fallback, next commit) needs
the reason — "the provider was down" and "the model took too long" and "our token was
rejected" are three different operational problems, and burying them all in one
``except`` would throw away the only signal that tells them apart.

ONE CLOCK, AND IT CANNOT BE UNDERCUT
------------------------------------
There are two timeouts in reach here, and only one of them may be in charge:

- **httpx's own** (connect / read / write / pool). Its default is **5 seconds**.
- **the reasoner's budget**, an ``asyncio.timeout`` around the whole call, ordered
  against the outbox worker's dispatch timeout in ``radar_common.timeouts``.

If httpx's is the shorter, IT is what actually fires — and every carefully-reasoned
number in ``radar_common.timeouts`` becomes decoration, because a third clock nobody
was thinking about is the one enforcing the bound. An ``extended``-mode call routinely
takes longer than 5 seconds, so with the httpx default every incident would fall back
to a template RCA and the service would look like it was working.

That failure is invisible: no crash, no error, just a fallback rate of 100% and an LLM
that never gets used. So it is made impossible rather than documented:
:class:`GatewayClient` REFUSES TO BE CONSTRUCTED with a client whose timeout could fire
before the budget. A misconfiguration is a startup failure, not a mystery in production.

The client is therefore built with ``timeout=None`` — httpx does not enforce anything,
``asyncio.timeout`` is the single bound, and there is exactly one number to reason
about. A hung connection is still bounded, because the ``asyncio`` timeout wraps the
whole await, not just the read.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum

import httpx
from pydantic import SecretStr, ValidationError
from radar_common import (
    AGENT_TOKEN_HEADER,
    REASONER_LLM_BUDGET_SECONDS,
    ConfigurationError,
    get_logger,
)
from radar_contracts import LLMMode, LLMRequest, LLMResponse, Message

from radar_reasoner_agent.context import ContextBundle

log = get_logger("reasoner.llm")

COMPLETE_PATH = "/v1/complete"

SYSTEM_PROMPT = """\
You are an SRE incident analysis assistant.
You will be given incident metadata and a structured investigation plan.
Respond ONLY with a valid JSON object. No text before or after it.

Schema:
{
  "root_cause": "your best assessment of the likely root cause",
  "confidence": "low|medium|high",
  "recommended_actions": [
    {"order": 1, "action": "specific actionable step"},
    {"order": 2, "action": "specific actionable step"}
  ]
}

Rules:
- Do not hallucinate metrics, log lines, or deployment names you were not given.
- If you cannot determine a root cause, set confidence=low and explain in root_cause.
- Actions must be specific, not generic. Bad: "check logs". Good: "check order-service
  error logs in Kibana for the last 30 minutes filtered by status=500".
"""
"""The v1 system prompt, verbatim from the implementation plan.

It asks for JSON and nothing else — but a model that returns prose anyway is not an
error the reasoner can prevent, only one it can survive. That is the parser's problem
(next commit) and the fallback's (the one after).
"""


class LLMFailureReason(StrEnum):
    """WHY the call failed. Three different operational problems, kept apart."""

    #: The gateway could not serve the request: every provider failed (503), the
    #: gateway itself errored (5xx), or we could not reach it at all. Nothing is wrong
    #: with RADAR — the LLM is simply unavailable, which is what the fallback is for.
    GATEWAY_UNAVAILABLE = "gateway_unavailable"

    #: The call outran the reasoner's budget. The model may still be generating; we
    #: stopped waiting. Distinguished from the above because it means the LLM is UP
    #: and slow, which is a capacity problem, not an outage.
    TIMEOUT = "timeout"

    #: The gateway rejected US: a bad token (401), a mode this token may not use
    #: (403), or a request it will not accept (422). This is OUR misconfiguration, it
    #: will fail identically on every incident, and it must be loud — a fallback rate
    #: of 100% with this reason means the reasoner has never once used the LLM.
    REJECTED = "rejected"


@dataclass(frozen=True, slots=True)
class LLMSuccess:
    """A completion came back. Whether it is USABLE is the parser's question."""

    content: str
    provider: str
    model: str
    mode: str
    prompt_tokens: int
    completion_tokens: int | None
    latency_ms: int


@dataclass(frozen=True, slots=True)
class LLMFailure:
    """No completion. ``reason`` is recorded on the fallback's context bundle."""

    reason: LLMFailureReason
    detail: str
    elapsed_ms: int


LLMResult = LLMSuccess | LLMFailure


class GatewayClient:
    """Calls ``POST /v1/complete`` in ``extended`` mode, within a hard budget.

    Refuses construction if ``client``'s own timeout could fire before ``budget`` — see
    the module docstring. That is the single most important line in this file: without
    it, httpx's 5-second default silently becomes the real bound and the LLM is never
    used at all.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: SecretStr,
        *,
        budget_seconds: float = REASONER_LLM_BUDGET_SECONDS,
    ) -> None:
        if budget_seconds <= 0:
            raise ConfigurationError("the LLM budget must be greater than zero")
        _reject_undercutting_timeout(client, budget_seconds)
        self._client = client
        self._token = token
        self._budget = budget_seconds

    async def complete(self, bundle: ContextBundle) -> LLMResult:
        """Ask the model for an RCA. Returns a result; never raises."""
        request = LLMRequest(
            mode=LLMMode.EXTENDED,
            messages=[
                Message(role="system", content=SYSTEM_PROMPT),
                Message(role="user", content=bundle.model_dump_json(indent=2)),
            ],
        )
        headers = {AGENT_TOKEN_HEADER: self._token.get_secret_value()}

        started = time.perf_counter()
        try:
            # THE bound. httpx enforces nothing (timeout=None), so this is the only
            # clock, and it is the one ordered against the worker's dispatch timeout.
            async with asyncio.timeout(self._budget):
                response = await self._client.post(
                    COMPLETE_PATH,
                    json=request.model_dump(mode="json"),
                    headers=headers,
                )
        except TimeoutError:
            return self._failure(
                LLMFailureReason.TIMEOUT,
                f"the LLM did not answer within {self._budget:g}s",
                started,
            )
        except httpx.TimeoutException:
            # Should be unreachable: the constructor rejects a client whose timeout
            # could fire first. If it happens, httpx is enforcing a bound nobody chose
            # — say so, rather than quietly reporting it as our timeout.
            return self._failure(
                LLMFailureReason.TIMEOUT,
                "httpx enforced a timeout shorter than the budget; the client is "
                "misconfigured and the LLM budget is not the bound in force",
                started,
            )
        except httpx.HTTPError as exc:
            return self._failure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                f"cannot reach the llm-gateway: {type(exc).__name__}",
                started,
            )

        return self._classify(response, started)

    def _classify(self, response: httpx.Response, started: float) -> LLMResult:
        elapsed = _elapsed_ms(started)

        if response.status_code == 503:
            # The gateway's own signal that every provider failed (ADR 0004's fallback
            # chain is exhausted). This is THE case the template fallback exists for.
            return LLMFailure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                "every LLM provider failed (gateway returned 503)",
                elapsed,
            )
        if response.status_code in (401, 403, 422):
            # OUR problem, and it will recur on every incident until someone fixes it.
            return LLMFailure(
                LLMFailureReason.REJECTED,
                f"the gateway rejected this request: HTTP {response.status_code}",
                elapsed,
            )
        if response.status_code >= 500 or response.status_code == 429:
            return LLMFailure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                f"the gateway is unhealthy: HTTP {response.status_code}",
                elapsed,
            )
        if response.status_code != 200:
            return LLMFailure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                f"unexpected gateway status: HTTP {response.status_code}",
                elapsed,
            )

        try:
            parsed = LLMResponse.model_validate_json(response.content)
        except ValidationError:
            # The gateway answered 200 with something that is not the gateway's own
            # contract. Not the model's fault and not parseable — treat the gateway as
            # unavailable rather than blame the RCA parser for a shape it never saw.
            return LLMFailure(
                LLMFailureReason.GATEWAY_UNAVAILABLE,
                "the gateway returned 200 with a body that is not an LLMResponse",
                elapsed,
            )

        return LLMSuccess(
            content=parsed.content,
            provider=parsed.provider,
            model=parsed.model,
            mode=parsed.mode.value,
            prompt_tokens=parsed.usage.prompt_tokens,
            completion_tokens=parsed.usage.completion_tokens,
            # The gateway measures its own latency; prefer it over ours, which includes
            # our network hop to the gateway.
            latency_ms=parsed.latency_ms,
        )

    def _failure(
        self, reason: LLMFailureReason, detail: str, started: float
    ) -> LLMFailure:
        failure = LLMFailure(reason, detail, _elapsed_ms(started))
        # WARNING, not error: a provider outage is not a RADAR bug, and the incident
        # still gets an RCA. REJECTED is the one that means something is broken here.
        log.warning(
            "llm.call_failed",
            reason=failure.reason.value,
            detail=failure.detail,
            elapsed_ms=failure.elapsed_ms,
        )
        return failure


def _elapsed_ms(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)


def _reject_undercutting_timeout(client: httpx.AsyncClient, budget: float) -> None:
    """Refuse a client whose own timeout could fire before the reasoner's budget.

    httpx defaults to 5 seconds. An ``extended``-mode call routinely takes longer, so a
    client left on the default would kill every call at 5s, every incident would fall
    back to a template RCA, and the service would look perfectly healthy while never
    once using the model it exists to use. There would be no error to find.

    So the two clocks are not allowed to disagree: either httpx enforces nothing
    (``timeout=None``, the intended configuration) or its timeout is at least the
    budget. Anything else fails at startup, where it is a config error somebody can
    read.
    """
    timeout = client.timeout
    for name in ("connect", "read", "write", "pool"):
        value = getattr(timeout, name, None)
        if value is not None and value < budget:
            raise ConfigurationError(
                f"the httpx client's {name} timeout ({value:g}s) is shorter than the "
                f"LLM budget ({budget:g}s), so httpx — not the budget — would be the "
                "bound actually in force. Build the client with timeout=None and let "
                "asyncio.timeout own the clock."
            )
