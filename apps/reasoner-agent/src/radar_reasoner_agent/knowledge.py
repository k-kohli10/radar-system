"""The knowledge-service client: graded runbook context for the RCA.

Calls ``POST /v1/context`` on the knowledge service with a copy of THAT
service's agent token (the caller presents the target's token, the same rule the
outbox worker follows with ``dispatch_tokens``). Never raises: retrieval is an
enhancement, and an incident must get an RCA whether or not it can be grounded.

THE THREE-WAY OUTCOME IS THE POINT
-----------------------------------
The knowledge service keeps "the corpus has nothing for this incident" (an empty
200 — CRAG's judgment) strictly apart from "retrieval is down" (a 503). This
client is where that distinction either survives into the reasoner or dies, so
its result is a three-valued outcome rather than a bare list:

- ``GROUNDED``     chunks came back; the RCA can cite runbooks.
- ``EMPTY``        the grader judged nothing relevant. The RCA proceeds
                   honestly ungrounded — the corpus does not cover this.
- ``UNAVAILABLE``  the call failed (timeout, transport, non-200, bad shape).
                   The RCA proceeds ungrounded, but the stored bundle says
                   retrieval FAILED rather than that coverage is absent.

Collapsing EMPTY and UNAVAILABLE here would waste everything upstream: CRAG's
gate, the API's 503, all of it exists so these two states stay distinguishable.

The budget lives in ``radar_common.timeouts`` because it participates in the
worker-dispatch ordering invariant: the knowledge fetch and the LLM call happen
sequentially on one dispatch, so the worker must outlast their SUM.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, SecretStr, ValidationError
from radar_common import AGENT_TOKEN_HEADER, ConfigurationError, get_logger
from radar_common.timeouts import REASONER_KNOWLEDGE_BUDGET_SECONDS

from radar_reasoner_agent.context import ContextBundle

log = get_logger("reasoner.knowledge")

CONTEXT_PATH = "/v1/context"


class RetrievalOutcome(StrEnum):
    """What the knowledge fetch produced. See the module docstring."""

    GROUNDED = "grounded"
    EMPTY = "empty"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class KnowledgeResult:
    """One fetch's outcome: the chunks (possibly none) and how they came to be."""

    outcome: RetrievalOutcome
    chunks: list[dict[str, Any]] = field(default_factory=list)
    #: Failure class or HTTP status for UNAVAILABLE; None otherwise. Never a raw
    #: vendor message.
    detail: str | None = None
    elapsed_ms: int | None = None

    def __post_init__(self) -> None:
        # The invariant the route leans on: chunks exist exactly when the
        # outcome is GROUNDED. Structural rather than conventional, because the
        # route copies `chunks` onto the bundle the MODEL reads — a result that
        # carried chunks alongside `unavailable` would quietly ground an RCA in
        # content nobody vouched for.
        grounded = self.outcome is RetrievalOutcome.GROUNDED
        if grounded != bool(self.chunks):
            raise ValueError(
                f"a {self.outcome.value} result must carry "
                f"{'chunks' if grounded else 'no chunks'}; got {len(self.chunks)}"
            )


class _ContextResponse(BaseModel):
    """The context API's response, validated rather than assumed."""

    model_config = ConfigDict(extra="ignore")

    chunks: list[dict[str, Any]]


class KnowledgeClient:
    """Fetches graded context, within a hard budget. Never raises."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: SecretStr,
        *,
        budget_seconds: float = REASONER_KNOWLEDGE_BUDGET_SECONDS,
    ) -> None:
        if budget_seconds <= 0:
            raise ConfigurationError("the knowledge budget must be greater than zero")
        _reject_undercutting_timeout(client, budget_seconds)
        self._client = client
        self._token = token
        self._budget = budget_seconds

    async def fetch(self, bundle: ContextBundle) -> KnowledgeResult:
        """Fetch graded context for the incident the bundle describes."""
        request = {
            "service_name": bundle.service_name,
            "alert_name": bundle.alert_name,
            "investigation_steps": [
                step.model_dump(mode="json") for step in bundle.investigation_steps
            ],
        }
        headers = {AGENT_TOKEN_HEADER: self._token.get_secret_value()}

        started = time.perf_counter()
        try:
            # THE bound. httpx enforces nothing (timeout=None); this is the only
            # clock, and it is ordered against the worker's dispatch timeout.
            async with asyncio.timeout(self._budget):
                response = await self._client.post(
                    CONTEXT_PATH, json=request, headers=headers
                )
        except TimeoutError:
            return self._unavailable("timeout", started)
        except httpx.HTTPError as exc:
            return self._unavailable(type(exc).__name__, started)

        if response.status_code != 200:
            # Includes the API's own 503 ("retrieval unavailable") and any
            # auth/validation failure: none of them is evidence about coverage.
            return self._unavailable(f"HTTP {response.status_code}", started)

        try:
            parsed = _ContextResponse.model_validate_json(response.content)
        except ValidationError:
            return self._unavailable("bad_response_shape", started)

        elapsed_ms = round((time.perf_counter() - started) * 1000)
        outcome = RetrievalOutcome.GROUNDED if parsed.chunks else RetrievalOutcome.EMPTY
        log.info(
            "reasoner.context_fetched",
            outcome=outcome.value,
            chunks=len(parsed.chunks),
            elapsed_ms=elapsed_ms,
        )
        return KnowledgeResult(
            outcome=outcome, chunks=parsed.chunks, elapsed_ms=elapsed_ms
        )

    def _unavailable(self, detail: str, started: float) -> KnowledgeResult:
        elapsed_ms = round((time.perf_counter() - started) * 1000)
        # WARNING, not error: the incident still gets an RCA. But it is the only
        # place "retrieval was down" is visible outside the stored bundle.
        log.warning(
            "reasoner.context_unavailable", detail=detail, elapsed_ms=elapsed_ms
        )
        return KnowledgeResult(
            outcome=RetrievalOutcome.UNAVAILABLE, detail=detail, elapsed_ms=elapsed_ms
        )


def _reject_undercutting_timeout(client: httpx.AsyncClient, budget: float) -> None:
    """Refuse a client whose own timeout could fire before the budget.

    Same guard as every gateway client in this repo, same reason: httpx's
    5-second default would silently become the real bound, and because this
    client degrades rather than raising, that would present as retrieval being
    permanently "unavailable" while everything looked healthy.
    """
    timeout = client.timeout
    for name in ("connect", "read", "write", "pool"):
        value = getattr(timeout, name, None)
        if value is not None and value < budget:
            raise ConfigurationError(
                f"the httpx client's {name} timeout ({value:g}s) is shorter than "
                f"the knowledge budget ({budget:g}s), so httpx — not the budget — "
                "would be the bound actually in force. Build the client with "
                "timeout=None and let asyncio.timeout own the clock."
            )
