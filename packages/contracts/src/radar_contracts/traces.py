"""Traces backend contract.

``TracesBackend`` is the vendor-neutral interface for emitting distributed
tracing spans (RADAR runs one span per request). It is a ``typing.Protocol``
(never an ABC) and references no vendor type; the OpenTelemetry SDK lives in the
telemetry package and the traces plugin, not here.

``start_span`` returns a synchronous context manager whose handle is a ``Span``:
attributes known upfront pass via ``attributes``, while those discovered during
the span (a status code, an exception) are set on the handle before the ``with``
block exits.
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractContextManager
from typing import Protocol, runtime_checkable


@runtime_checkable
class Span(Protocol):
    """A handle to an in-progress span."""

    def set_attribute(self, key: str, value: str) -> None:
        """Attach a single attribute to the span."""
        ...

    def record_exception(self, exception: BaseException) -> None:
        """Record an exception as an event on the span."""
        ...


@runtime_checkable
class TracesBackend(Protocol):
    """Interface for a tracing backend."""

    def start_span(
        self,
        name: str,
        *,
        attributes: Mapping[str, str] | None = None,
    ) -> AbstractContextManager[Span]:
        """Start span ``name`` and return it as a context manager.

        The span ends when the returned context manager exits. Attributes known
        at start may be supplied via ``attributes``; others are set on the
        yielded ``Span`` handle.
        """
        ...
