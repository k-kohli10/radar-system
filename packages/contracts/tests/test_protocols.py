"""Structural-conformance tests for the radar_contracts backend Protocols.

Every backend interface is a ``@runtime_checkable`` ``typing.Protocol``: a
duck-typed object that provides the methods satisfies ``isinstance``, and one
that does not, fails. No ABC inheritance is involved.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any

from radar_contracts import (
    EmbeddingProvider,
    GatewayStreamEvent,
    KnowledgeStore,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LogsBackend,
    MetricsBackend,
    NotificationBackend,
    Span,
    TracesBackend,
)


class FakeLLMProvider:
    async def complete(self, request: LLMRequest) -> LLMResponse:  # pragma: no cover
        raise NotImplementedError

    def stream(
        self, request: LLMRequest
    ) -> AsyncIterator[GatewayStreamEvent]:  # pragma: no cover
        raise NotImplementedError


class FakeEmbeddingProvider:
    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [[0.0] for _ in texts]


class FakeKnowledgeStore:
    async def index(self, documents: list[dict[str, Any]]) -> int:
        return len(documents)

    async def retrieve(
        self,
        query: str,
        *,
        service_name: str | None = None,
        limit: int = 5,
    ) -> list[dict[str, Any]]:
        return []


class FakeLogsBackend:
    async def query(
        self,
        service_name: str,
        *,
        query: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        return []


class FakeMetricsBackend:
    def increment_counter(
        self, name: str, *, labels: Mapping[str, str] | None = None, value: float = 1.0
    ) -> None: ...

    def observe_histogram(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None: ...

    def set_gauge(
        self, name: str, value: float, *, labels: Mapping[str, str] | None = None
    ) -> None: ...


class FakeSpan:
    def set_attribute(self, key: str, value: str) -> None: ...

    def record_exception(self, exception: BaseException) -> None: ...


class FakeTracesBackend:
    @contextmanager
    def start_span(
        self, name: str, *, attributes: Mapping[str, str] | None = None
    ) -> Any:
        yield FakeSpan()


class FakeNotificationBackend:
    async def send(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ref: str | None = None,
    ) -> str:
        return "ts.1"

    async def update(
        self,
        channel: str,
        message_ref: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None: ...


class NotAnything:
    def unrelated(self) -> None: ...


def test_conforming_objects_satisfy_their_protocol() -> None:
    assert isinstance(FakeLLMProvider(), LLMProvider)
    assert isinstance(FakeEmbeddingProvider(), EmbeddingProvider)
    assert isinstance(FakeKnowledgeStore(), KnowledgeStore)
    assert isinstance(FakeLogsBackend(), LogsBackend)
    assert isinstance(FakeMetricsBackend(), MetricsBackend)
    assert isinstance(FakeTracesBackend(), TracesBackend)
    assert isinstance(FakeSpan(), Span)
    assert isinstance(FakeNotificationBackend(), NotificationBackend)


def test_nonconforming_object_fails_every_protocol() -> None:
    obj = NotAnything()
    for protocol in (
        LLMProvider,
        EmbeddingProvider,
        KnowledgeStore,
        LogsBackend,
        MetricsBackend,
        TracesBackend,
        NotificationBackend,
    ):
        assert not isinstance(obj, protocol)
