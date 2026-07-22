"""Embeddings, via the gateway, never a provider SDK.

This service holds no OpenAI key and imports no provider client. It asks
``llm-gateway`` for vectors in ``embed`` mode with a token scoped to exactly that
mode, so swapping the embedding model is gateway config — with one caveat that is
not free: the vector dimension is baked into the Elasticsearch ``dense_vector``
mapping, so a model with different dimensions means a new index and a re-index.
:class:`GatewayEmbeddingClient` therefore checks the dimension of what comes back
and refuses anything else, rather than letting a silent model change reach the
bulk request and be rejected one document at a time.

RAISES, RATHER THAN RETURNING A RESULT
--------------------------------------
The reasoner's gateway client returns a typed *result* because an incident must
get an RCA either way — a template fallback exists. Embedding has no such
fallback: a chunk without a vector cannot be indexed, and indexing it without one
would put a silently unretrievable document in the index. So failures raise, and
each caller decides:

- the **indexer** lets them propagate — a failed run is a failed run, retried
  later, leaving the index exactly as it was;
- **query-time** embedding catches them and degrades to empty
  ``retrieved_context``, because an incident still needs an answer.

ONE CLOCK
---------
Same rule as the reasoner: ``asyncio.timeout`` is the only bound, httpx is built
with ``timeout=None``, and the constructor refuses a client whose own timeout
could fire first. Two clocks that disagree means the one nobody reasoned about is
the one in force.

The budget here is deliberately the caller's own ceiling rather than trust in the
gateway's: the gateway's ``embed`` mode allows 10s per provider attempt and may
retry three times with backoff, so a pathological call could outlast anything the
indexer wants to wait for.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError
from radar_common import (
    AGENT_TOKEN_HEADER,
    ConfigurationError,
    estimate_tokens,
    get_logger,
)
from radar_contracts import LLMMode

log = get_logger("knowledge.embeddings")

EMBED_PATH = "/v1/embed"

EMBED_BUDGET_SECONDS: float = 30.0
"""Wall-clock ceiling for one batch of embeddings.

Generous against the gateway's 10s-per-attempt ``embed`` mode so a single retry
does not trip it, but bounded so a hung provider cannot stall an indexing run
indefinitely. The knowledge service owns this number alone — unlike the
reasoner/worker pair in ``radar_common.timeouts``, no other service's timeout has
to be ordered against it.
"""

DEFAULT_BATCH_SIZE = 64
"""Inputs per gateway request. Bounded so one oversized batch cannot be built."""

#: The gateway's ``embed`` mode per-input limit (``config/gateway.yaml``).
DEFAULT_MAX_INPUT_TOKENS = 8191


class EmbeddingError(RuntimeError):
    """Base for every way embedding can fail."""


class EmbeddingRejectedError(EmbeddingError):
    """The gateway refused us: bad token, wrong mode, or an unacceptable input.

    Our misconfiguration. It will fail identically on every run until fixed, so
    it must be loud rather than retried into forever.
    """


class EmbeddingUnavailableError(EmbeddingError):
    """The gateway could not serve the request, or could not be reached.

    Not our bug — retry later. The index is unchanged.
    """


class EmbeddingTimeoutError(EmbeddingError):
    """The call outran this client's budget. The gateway may still be working."""


class EmbeddingContractError(EmbeddingError):
    """The gateway answered 200 with something unusable.

    Wrong vector count or wrong dimension. Raised rather than tolerated because
    both corrupt the index silently: a short list misaligns chunks against
    vectors, and a wrong dimension is a model change the index cannot hold.
    """


class _EmbedResponse(BaseModel):
    """The gateway's ``/v1/embed`` response, validated rather than assumed."""

    model_config = ConfigDict(extra="ignore")

    embeddings: list[list[float]] = Field(min_length=1)
    model: str


def estimate_input_tokens(text: str) -> int:
    """Estimate one input's token count.

    Embedding limits are per input, not per batch, so this wraps the shared
    estimator with a one-element tuple — the same call the gateway's own
    ``enforce_embed_budget`` makes, from the same function, so the pre-check
    here and the enforcement there cannot disagree.
    """
    return estimate_tokens((text,))


class GatewayEmbeddingClient:
    """Embeds text through ``POST /v1/embed``, in batches, within a hard budget."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        token: SecretStr,
        *,
        dims: int,
        max_input_tokens: int = DEFAULT_MAX_INPUT_TOKENS,
        batch_size: int = DEFAULT_BATCH_SIZE,
        budget_seconds: float = EMBED_BUDGET_SECONDS,
    ) -> None:
        """Bind to a gateway, an expected dimension, and a per-batch budget.

        ``dims`` is what the Elasticsearch mapping was created with. Checking
        returned vectors against it is what turns a silent model swap into a
        loud failure at the first batch.
        """
        if budget_seconds <= 0:
            raise ConfigurationError("the embedding budget must be greater than zero")
        if batch_size <= 0:
            raise ConfigurationError("the embedding batch size must be at least one")
        _reject_undercutting_timeout(client, budget_seconds)
        self._client = client
        self._token = token
        self._dims = dims
        self._max_input_tokens = max_input_tokens
        self._batch_size = batch_size
        self._budget = budget_seconds

    @property
    def dims(self) -> int:
        return self._dims

    def check_budget(self, texts: list[str]) -> None:
        """Raise if any input exceeds the mode's per-input token limit.

        This is the assertion the chunker deliberately does not make: the
        chunker is pure and knows nothing about a model's limits, while this
        client does. Embedding limits are per input, not per batch, so an
        oversized chunk is named by position rather than failing the batch
        anonymously.
        """
        for position, text in enumerate(texts):
            estimated = estimate_input_tokens(text)
            if estimated > self._max_input_tokens:
                raise EmbeddingRejectedError(
                    f"input {position} is ~{estimated} tokens, over the embed "
                    f"per-input limit of {self._max_input_tokens}. A runbook "
                    f"section grew past what one chunk can hold — split it at a "
                    f"`###` boundary (see docs/runbooks/README.md)."
                )

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed ``texts``, returning one vector per input, in input order.

        Batches internally; the returned list always lines up with ``texts``
        one-for-one, which is the property the indexer relies on to pair chunks
        with their vectors.
        """
        if not texts:
            return []

        self.check_budget(texts)

        vectors: list[list[float]] = []
        for start in range(0, len(texts), self._batch_size):
            batch = texts[start : start + self._batch_size]
            vectors.extend(await self._embed_batch(batch))

        if len(vectors) != len(texts):
            raise EmbeddingContractError(
                f"asked for {len(texts)} embeddings and assembled {len(vectors)}"
            )
        return vectors

    async def _embed_batch(self, batch: list[str]) -> list[list[float]]:
        payload: dict[str, Any] = {"mode": LLMMode.EMBED.value, "input": batch}
        headers = {AGENT_TOKEN_HEADER: self._token.get_secret_value()}

        started = time.perf_counter()
        try:
            # THE bound: httpx enforces nothing, this is the only clock.
            async with asyncio.timeout(self._budget):
                response = await self._client.post(
                    EMBED_PATH, json=payload, headers=headers
                )
        except TimeoutError as exc:
            raise EmbeddingTimeoutError(
                f"the gateway did not return embeddings within {self._budget:g}s"
            ) from exc
        except httpx.TimeoutException as exc:
            # Unreachable unless the constructor's guard was bypassed.
            raise EmbeddingTimeoutError(
                "httpx enforced a timeout shorter than the budget; the client is "
                "misconfigured and the budget is not the bound in force"
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingUnavailableError(
                f"cannot reach the llm-gateway: {type(exc).__name__}"
            ) from exc

        return self._parse(response, len(batch), started)

    def _parse(
        self, response: httpx.Response, expected: int, started: float
    ) -> list[list[float]]:
        if response.status_code in (401, 403, 422):
            raise EmbeddingRejectedError(
                f"the gateway rejected this request: HTTP {response.status_code}"
            )
        if response.status_code != 200:
            raise EmbeddingUnavailableError(
                f"the gateway could not serve embeddings: HTTP {response.status_code}"
            )

        try:
            parsed = _EmbedResponse.model_validate_json(response.content)
        except ValidationError as exc:
            raise EmbeddingContractError(
                "the gateway returned 200 with a body that is not an embed response"
            ) from exc

        if len(parsed.embeddings) != expected:
            # Chunks are paired with vectors BY POSITION. A short or long list
            # would attach the wrong vector to a chunk and make the index quietly
            # wrong rather than visibly broken.
            raise EmbeddingContractError(
                f"asked for {expected} embeddings, got {len(parsed.embeddings)}"
            )

        for position, vector in enumerate(parsed.embeddings):
            if len(vector) != self._dims:
                raise EmbeddingContractError(
                    f"embedding {position} has {len(vector)} dimensions, but the "
                    f"index expects {self._dims}. The gateway's embed model has "
                    f"changed: this needs a new index and a full re-index, not a "
                    f"config change (model reported: {parsed.model!r})."
                )

        log.debug(
            "knowledge.embedded_batch",
            count=expected,
            model=parsed.model,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return parsed.embeddings


def _reject_undercutting_timeout(client: httpx.AsyncClient, budget: float) -> None:
    """Refuse a client whose own timeout could fire before the budget.

    httpx defaults to 5 seconds. A batch of 64 embeddings routinely takes longer,
    so a client left on the default would abort every batch at 5s and indexing
    would fail for a reason that looks like the gateway being slow. Same guard,
    and same reasoning, as the reasoner's gateway client.
    """
    timeout = client.timeout
    for name in ("connect", "read", "write", "pool"):
        value = getattr(timeout, name, None)
        if value is not None and value < budget:
            raise ConfigurationError(
                f"the httpx client's {name} timeout ({value:g}s) is shorter than "
                f"the embedding budget ({budget:g}s), so httpx — not the budget — "
                "would be the bound actually in force. Build the client with "
                "timeout=None and let asyncio.timeout own the clock."
            )
