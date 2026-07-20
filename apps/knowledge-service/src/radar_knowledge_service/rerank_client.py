"""The rerank gateway call: the I/O half of reranking.

Calls ``POST /v1/complete`` in ``reason`` mode with a token granted exactly that
mode — a SECOND token, separate from the ``embed`` one, so a leaked embedding
credential cannot be spent on reasoning. All the decisions live in
:mod:`radar_knowledge_service.reranking`; this module only makes the call.

DEGRADES, RATHER THAN RAISING
-----------------------------
Unlike embedding, a failed rerank is survivable: the fused ordering is already a
usable answer, and reranking only improves it. So every failure path returns the
candidates unchanged rather than propagating, and the reason is logged.

The alternative — raising — would mean one flaky LLM call turns a retrievable
incident into no context at all, trading a slightly worse ranking for no ranking.
That is the wrong direction. This mirrors the reasoner's gateway client, which
returns a result rather than raising because an incident must get an RCA either
way.

It is a deliberate consequence that reranking failing is INVISIBLE in the result
and visible only in logs and metrics. That is why the log line names the reason
rather than saying "rerank failed": a stage that silently does nothing is exactly
the failure this codebase keeps designing against, and the log is the only place
it can surface.

ONE CLOCK
---------
Same rule as the embedding client: ``asyncio.timeout`` is the only bound, httpx
is built with ``timeout=None``, and the constructor refuses a client whose own
timeout could fire first. Two clocks that disagree means the one nobody reasoned
about is the one in force.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from radar_common import AGENT_TOKEN_HEADER, ConfigurationError, get_logger
from radar_contracts import LLMMode

from radar_knowledge_service.reranking import (
    RERANK_SYSTEM_PROMPT,
    RerankParseError,
    build_rerank_prompt,
    parse_rerank_scores,
    rerank_by_scores,
)

log = get_logger("knowledge.rerank")

COMPLETE_PATH = "/v1/complete"

RERANK_BUDGET_SECONDS: float = 30.0
"""Wall-clock ceiling for one rerank call.

Matches the gateway's ``reason`` mode timeout of 30s. Reranking sits in the
incident path, so this is a ceiling on how long an improvement stage may delay an
answer it is only improving.
"""


class _CompleteResponse(BaseModel):
    """The gateway's ``/v1/complete`` response, validated rather than assumed."""

    model_config = ConfigDict(extra="ignore")

    content: str = Field(min_length=1)


class GatewayReranker:
    """Reorders candidates by LLM relevance score, in one batched call."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: SecretStr,
        *,
        budget_seconds: float = RERANK_BUDGET_SECONDS,
    ) -> None:
        if budget_seconds <= 0:
            raise ConfigurationError("the rerank budget must be greater than zero")
        _reject_undercutting_timeout(client, budget_seconds)
        self._client = client
        self._token = token
        self._budget = budget_seconds

    async def rerank(
        self, query: str, candidates: list[dict[str, Any]], *, limit: int | None = None
    ) -> list[dict[str, Any]]:
        """Return ``candidates`` reordered by relevance. Never raises.

        On any failure the candidates come back in the order they arrived,
        truncated to ``limit`` — the fused ordering, which is what retrieval
        would have returned without this stage.
        """
        if not candidates:
            return []

        request = {
            "mode": LLMMode.REASON.value,
            "messages": [
                {"role": "system", "content": RERANK_SYSTEM_PROMPT},
                {"role": "user", "content": build_rerank_prompt(query, candidates)},
            ],
        }
        headers = {AGENT_TOKEN_HEADER: self._token.get_secret_value()}

        started = time.perf_counter()
        try:
            # THE bound. httpx enforces nothing (timeout=None), so this is the
            # only clock.
            async with asyncio.timeout(self._budget):
                response = await self._client.post(
                    COMPLETE_PATH, json=request, headers=headers
                )
        except TimeoutError:
            return self._degrade(candidates, limit, "timeout", budget=self._budget)
        except httpx.HTTPError as exc:
            return self._degrade(
                candidates, limit, "transport", error=type(exc).__name__
            )

        if response.status_code != 200:
            # Status only, never the body: a gateway error body can carry vendor
            # detail, and this log line is not the place for it.
            return self._degrade(
                candidates, limit, "http_error", status=response.status_code
            )

        try:
            parsed = _CompleteResponse.model_validate_json(response.content)
        except ValidationError:
            return self._degrade(candidates, limit, "bad_response_shape")

        try:
            scores = parse_rerank_scores(parsed.content)
        except RerankParseError as exc:
            return self._degrade(
                candidates, limit, "unparseable_scores", error=str(exc)
            )

        reordered = rerank_by_scores(candidates, scores, limit=limit)
        log.info(
            "knowledge.reranked",
            candidates=len(candidates),
            scored=len(scores),
            returned=len(reordered),
            elapsed_ms=round((time.perf_counter() - started) * 1000),
            # Whether the top result changed is the one number that says if this
            # stage did anything at all on this call.
            top_changed=reordered[0]["chunk_id"] != candidates[0]["chunk_id"],
        )
        return reordered

    def _degrade(
        self,
        candidates: list[dict[str, Any]],
        limit: int | None,
        reason: str,
        **detail: Any,
    ) -> list[dict[str, Any]]:
        """Fall back to the fused ordering, saying why."""
        log.warning(
            "knowledge.rerank_skipped",
            reason=reason,
            candidates=len(candidates),
            **detail,
        )
        return candidates if limit is None else candidates[:limit]


def _reject_undercutting_timeout(client: httpx.AsyncClient, budget: float) -> None:
    """Refuse a client whose own timeout could fire before the budget.

    httpx defaults to 5 seconds and a ``reason``-mode completion routinely takes
    longer, so a client left on the default would abort every rerank at 5s. The
    stage degrades silently, so this would present as reranking simply never
    doing anything — the same guard, and the same reasoning, as the embedding
    client.
    """
    timeout = client.timeout
    for name in ("connect", "read", "write", "pool"):
        value = getattr(timeout, name, None)
        if value is not None and value < budget:
            raise ConfigurationError(
                f"the httpx client's {name} timeout ({value:g}s) is shorter than "
                f"the rerank budget ({budget:g}s), so httpx — not the budget — "
                "would be the bound actually in force. Build the client with "
                "timeout=None and let asyncio.timeout own the clock."
            )
