"""The CRAG grading call: the I/O half.

Calls ``POST /v1/complete`` in ``reason`` mode with the knowledge service's
second gateway token, originally granted for the reranking stage that was
measured and removed. The decisions live in :mod:`radar_knowledge_service.crag`.

DEGRADES TO UNGRADED RETRIEVAL, WHICH IS THE CONSERVATIVE DIRECTION
--------------------------------------------------------------------
Every failure returns the chunks unchanged and ungraded, and logs why. Failing
the retrieval would let a flaky LLM call cost an incident its context entirely
when retrieval had already produced a usable answer. Returning empty would be
worse still: empty context is a CLAIM that the corpus has nothing for this
incident, and a transport error is no evidence for it.

The caller can tell the difference: a graded result carries ``grade`` on every
chunk, an ungraded one carries none.

ONE CLOCK
---------
``asyncio.timeout`` is the only bound, httpx is built with ``timeout=None``, and
the constructor refuses a client whose own timeout could fire first, the same
guard as the embedding client.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from radar_common import AGENT_TOKEN_HEADER, ConfigurationError, get_logger
from radar_contracts import LLMMode

from radar_knowledge_service.crag import (
    CRAG_SYSTEM_PROMPT,
    CragParseError,
    apply_grades,
    build_grading_prompt,
    parse_grades,
)

log = get_logger("knowledge.crag")

COMPLETE_PATH = "/v1/complete"

CRAG_BUDGET_SECONDS: float = 30.0
"""Wall-clock ceiling for one grading call, matching the gateway's reason mode."""


class _CompleteResponse(BaseModel):
    """The gateway's ``/v1/complete`` response, validated rather than assumed."""

    model_config = ConfigDict(extra="ignore")

    content: str = Field(min_length=1)


class GatewayGrader:
    """Grades retrieved chunks against the incident, in one batched call."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: SecretStr,
        *,
        budget_seconds: float = CRAG_BUDGET_SECONDS,
    ) -> None:
        if budget_seconds <= 0:
            raise ConfigurationError("the CRAG budget must be greater than zero")
        _reject_undercutting_timeout(client, budget_seconds)
        self._client = client
        self._token = token
        self._budget = budget_seconds

    async def grade(
        self, query: str, chunks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        """Return the usable chunks, each carrying its grade. Never raises.

        An EMPTY list means the model judged every chunk insufficient, which is
        the stage's whole purpose. It never means the call failed; failures
        return the chunks ungraded.
        """
        if not chunks:
            return []

        chunk_ids = [chunk["chunk_id"] for chunk in chunks]
        request = {
            "mode": LLMMode.REASON.value,
            "messages": [
                {"role": "system", "content": CRAG_SYSTEM_PROMPT},
                {"role": "user", "content": build_grading_prompt(query, chunks)},
            ],
        }
        headers = {AGENT_TOKEN_HEADER: self._token.get_secret_value()}

        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._budget):
                response = await self._client.post(
                    COMPLETE_PATH, json=request, headers=headers
                )
        except TimeoutError:
            return self._degrade(chunks, "timeout", budget=self._budget)
        except httpx.HTTPError as exc:
            return self._degrade(chunks, "transport", error=type(exc).__name__)

        if response.status_code != 200:
            return self._degrade(chunks, "http_error", status=response.status_code)

        try:
            parsed = _CompleteResponse.model_validate_json(response.content)
        except ValidationError:
            return self._degrade(chunks, "bad_response_shape")

        try:
            grades = parse_grades(parsed.content, chunk_ids)
        except CragParseError as exc:
            return self._degrade(chunks, "unparseable_grades", error=str(exc))

        kept = apply_grades(chunks, grades)
        log.info(
            "knowledge.graded",
            chunks=len(chunks),
            kept=len(kept),
            # The stage's meaningful output, logged as its own signal: an empty
            # context is a claim about the corpus and should be visible as one.
            empty_context=not kept,
            elapsed_ms=round((time.perf_counter() - started) * 1000),
        )
        return kept

    def _degrade(
        self, chunks: list[dict[str, Any]], reason: str, **detail: Any
    ) -> list[dict[str, Any]]:
        """Return the chunks ungraded, saying why.

        Deliberately NOT empty: empty is a claim that nothing is relevant, and a
        failed call is no evidence for it.
        """
        log.warning(
            "knowledge.grading_skipped", reason=reason, chunks=len(chunks), **detail
        )
        return chunks


def _reject_undercutting_timeout(client: httpx.AsyncClient, budget: float) -> None:
    """Refuse a client whose own timeout could fire before the budget."""
    timeout = client.timeout
    for name in ("connect", "read", "write", "pool"):
        value = getattr(timeout, name, None)
        if value is not None and value < budget:
            raise ConfigurationError(
                f"the httpx client's {name} timeout ({value:g}s) is shorter than "
                f"the CRAG budget ({budget:g}s), so httpx — not the budget — "
                "would be the bound actually in force. Build the client with "
                "timeout=None and let asyncio.timeout own the clock."
            )
