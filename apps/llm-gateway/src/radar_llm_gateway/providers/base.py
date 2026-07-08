"""The gateway-side provider binding.

The SDK adapters live in ``plugins/llm/*`` and implement the vendor-neutral
``radar-contracts`` protocols, letting their vendor exceptions propagate.
:class:`ProviderBinding` is how the gateway pipeline calls them: one binding
wraps one plugin instance already constructed for a concrete model, and adds
the two behaviors every call needs regardless of vendor:

- **Hard timeout** — the mode's ``timeout_seconds`` via ``asyncio.timeout``,
  surfacing as :class:`ProviderTimeoutError` (retryable). For streams the
  deadline covers the whole stream.
- **Vendor-exception translation** — via a per-vendor
  :data:`FailureTranslator` from ``providers/{vendor}_provider.py``.

Redaction is enforced here *by construction*: a translator can only classify a
failure (:class:`FailureInfo`: status code, retryable, timeout) — it cannot
supply text. The :class:`ProviderError` reason is always built in this module
from the vendor exception's **class name** alone, and errors are raised
``from None`` so the vendor exception (whose message can echo prompt content)
never rides along in tracebacks or logs. Vendor messages are dropped entirely,
never truncated.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass

from radar_contracts import (
    EmbeddingProvider,
    GatewayStreamEvent,
    LLMProvider,
    LLMRequest,
    LLMResponse,
)

from radar_llm_gateway.core.errors import ProviderError, ProviderTimeoutError


@dataclass(frozen=True)
class FailureInfo:
    """A vendor translator's classification of one vendor exception.

    Deliberately holds no message text; ``timeout=True`` marks vendor-side
    connect/read timeouts so they surface as :class:`ProviderTimeoutError`.
    """

    status_code: int | None = None
    retryable: bool | None = None
    timeout: bool = False


type FailureTranslator = Callable[[BaseException], FailureInfo | None]
"""Maps a vendor exception to its classification, or None if unrecognized."""


class ProviderBinding:
    """One provider plugin instance, bound to one model, safe to call.

    Built by the model router: chat modes get ``chat``, embed modes get
    ``embedder``. Calling a capability the binding was not built with is a
    routing bug and raises ``RuntimeError``.
    """

    def __init__(
        self,
        *,
        provider_name: str,
        model: str,
        timeout_seconds: float,
        translate: FailureTranslator,
        chat: LLMProvider | None = None,
        embedder: EmbeddingProvider | None = None,
    ) -> None:
        self.provider_name = provider_name
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._translate = translate
        self._chat = chat
        self._embedder = embedder

    def __repr__(self) -> str:
        return f"ProviderBinding({self.provider_name}/{self.model})"

    def _timeout_error(self) -> ProviderTimeoutError:
        return ProviderTimeoutError(
            self.provider_name, self.model, self.timeout_seconds
        )

    def _translated(self, exc: BaseException) -> ProviderError:
        info = self._translate(exc) or FailureInfo()
        if info.timeout:
            return self._timeout_error()
        return ProviderError(
            self.provider_name,
            self.model,
            status_code=info.status_code,
            retryable=info.retryable,
            # Class name only: vendor messages can echo prompt content and are
            # dropped entirely (never truncated).
            reason=type(exc).__name__,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion under the timeout; raise only gateway errors."""
        if self._chat is None:
            raise RuntimeError(f"{self!r} has no chat capability")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._chat.complete(request)
        except TimeoutError:
            raise self._timeout_error() from None
        except ProviderError:
            raise
        except Exception as exc:
            raise self._translated(exc) from None

    async def stream(self, request: LLMRequest) -> AsyncIterator[GatewayStreamEvent]:
        """Stream a completion; the timeout is a deadline for the whole stream."""
        if self._chat is None:
            raise RuntimeError(f"{self!r} has no chat capability")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async for event in self._chat.stream(request):
                    yield event
        except TimeoutError:
            raise self._timeout_error() from None
        except ProviderError:
            raise
        except Exception as exc:
            raise self._translated(exc) from None

    async def embed(self, texts: list[str]) -> list[list[float]]:
        """Run one embedding call under the timeout; raise only gateway errors."""
        if self._embedder is None:
            raise RuntimeError(f"{self!r} has no embedding capability")
        try:
            async with asyncio.timeout(self.timeout_seconds):
                return await self._embedder.embed(texts)
        except TimeoutError:
            raise self._timeout_error() from None
        except ProviderError:
            raise
        except Exception as exc:
            raise self._translated(exc) from None
