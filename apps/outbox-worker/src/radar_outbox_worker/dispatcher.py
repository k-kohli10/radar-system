"""The outbound dispatcher: deliver one claimed event via ``POST /events``.

The poller hands each claimed event here. The dispatcher resolves the target
service to a URL, POSTs the delivery envelope with the worker's own
``X-Radar-Agent-Token``, enforces a hard 10-second wall-clock bound, and returns
a :class:`DispatchResult` classifying the outcome. It does **not** touch the
database or schedule retries — it only performs the HTTP call and reports what
happened; the poller's handler acts on the result (mark delivered, reschedule,
or dead-letter) in a later commit.

Retry classification deliberately **mirrors the Phase 4 LLM-gateway retry policy**
(see its spec in the implementation plan) so the whole platform treats upstream
failures consistently:

- **Retryable** (transient — back off and try again): a client-side timeout or
  connection error, ``429``, or ``500/502/503/504``.
- **Permanent** (will never succeed on retry — dead-letter immediately): ``400``,
  ``401``, ``403``, ``422`` (and any other non-2xx we did not mark retryable).

``401`` and ``422`` are tagged with *distinct* reasons on purpose: a ``401`` means
the worker's own agent token is rejected by the target — a misconfiguration that
will fail *every* dispatch to that service — whereas a ``422`` means one malformed
event. Operationally different, so they must be distinguishable in logs and (a
later commit) metrics.

The delivery body is the documented ``POST /events`` contract — exactly
``event_id``, ``event_type``, ``correlation_id``, ``payload`` — not the full
outbox row (whose internal ``status``/``attempts``/row ``id`` must never cross the
wire). A shared delivery-envelope contract belongs in Phase 7, when the receiving
agents' ``/events`` endpoints exist to share it.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import httpx
from pydantic import SecretStr
from radar_common import AGENT_TOKEN_HEADER, ConfigurationError, get_logger
from radar_database import OutboxEvent
from radar_telemetry import OutboxMetrics

#: Hard per-dispatch wall-clock budget. Enforced with ``asyncio.timeout`` around
#: the whole request so a slow-drip response cannot outlast it the way httpx's
#: per-read timeout can.
DISPATCH_TIMEOUT_SECONDS: Final = 10.0

#: k8s in-cluster default; ``{service}`` is filled with the event's
#: ``target_service``. Overridable per target via config (dev/compose/tests use
#: the same resolution path, never a test-only branch).
DEFAULT_URL_TEMPLATE: Final = "http://{service}.radar.svc.cluster.local:8080/events"

#: Transient statuses worth retrying — mirrors the gateway retry policy.
RETRYABLE_STATUS_CODES: Final[frozenset[int]] = frozenset({429, 500, 502, 503, 504})

#: Permanent statuses, each with a distinct reason tag. 401 (broken worker token,
#: every dispatch to the target will fail) must be distinguishable from 422 (one
#: malformed event). Any other non-2xx not marked retryable is permanent too.
PERMANENT_STATUS_REASONS: Final[dict[int, str]] = {
    400: "bad_request",
    401: "auth_rejected",
    403: "forbidden",
    422: "unprocessable",
}

log = get_logger("outbox-worker.dispatcher")


class DispatchStatus(StrEnum):
    """The outcome class of a single dispatch attempt."""

    DELIVERED = "delivered"
    RETRYABLE = "retryable"
    PERMANENT = "permanent"


@dataclass(frozen=True, slots=True)
class DispatchResult:
    """What one dispatch attempt did, for the handler to act on.

    ``reason`` is a short stable tag (``"ok"``, ``"timeout"``, ``"server_error"``,
    ``"auth_rejected"``, ``"unprocessable"``, …) used for log/metric labels and to
    distinguish failure modes. ``detail`` is a secret-free human string stored in
    ``outbox_events.last_error``; it never contains the response body or token.
    """

    status: DispatchStatus
    reason: str
    status_code: int | None
    detail: str

    @property
    def delivered(self) -> bool:
        return self.status is DispatchStatus.DELIVERED

    @property
    def retryable(self) -> bool:
        return self.status is DispatchStatus.RETRYABLE

    @property
    def permanent(self) -> bool:
        return self.status is DispatchStatus.PERMANENT


class TargetResolver:
    """Maps an event's ``target_service`` to its ``POST /events`` URL.

    A single ``url_template`` (the k8s default) with an optional per-target
    ``overrides`` map, both supplied by config, so local/dev/test point targets
    elsewhere through the exact same code path production uses.
    """

    def __init__(
        self,
        *,
        url_template: str = DEFAULT_URL_TEMPLATE,
        overrides: dict[str, str] | None = None,
    ) -> None:
        if "{service}" not in url_template:
            raise ConfigurationError(
                "dispatch URL template must contain the '{service}' placeholder"
            )
        self._template = url_template
        self._overrides = dict(overrides or {})

    def resolve(self, target_service: str) -> str:
        """Return the dispatch URL for ``target_service``."""
        override = self._overrides.get(target_service)
        if override is not None:
            return override
        return self._template.format(service=target_service)


class EventDispatcher:
    """Delivers claimed events over HTTP, classifying each outcome.

    The ``httpx.AsyncClient`` is injected so its connection pool and lifecycle are
    owned by the app (opened/closed in the lifespan), and shared across dispatches.
    """

    def __init__(
        self,
        client: httpx.AsyncClient,
        resolver: TargetResolver,
        agent_token: SecretStr,
        *,
        timeout_seconds: float = DISPATCH_TIMEOUT_SECONDS,
        metrics: OutboxMetrics | None = None,
    ) -> None:
        self._client = client
        self._resolver = resolver
        self._agent_token = agent_token
        self._timeout_seconds = timeout_seconds
        self._metrics = metrics

    async def dispatch(self, event: OutboxEvent) -> DispatchResult:
        """POST one event to its target and return the classified outcome."""
        url = self._resolver.resolve(event.target_service)
        body = {
            "event_id": str(event.event_id),
            "event_type": event.event_type,
            "correlation_id": str(event.correlation_id),
            "payload": event.payload,
        }
        headers = {AGENT_TOKEN_HEADER: self._agent_token.get_secret_value()}

        started = time.perf_counter()
        try:
            async with asyncio.timeout(self._timeout_seconds):
                response = await self._client.post(url, json=body, headers=headers)
        except TimeoutError:
            # asyncio hard wall-clock bound fired.
            result = self._timeout_result(event)
        except httpx.TimeoutException:
            # httpx's own connect/read timeout (before the wall-clock bound).
            result = self._timeout_result(event)
        except httpx.HTTPError as exc:
            # Connection refused/reset/DNS — transient; the target may be rolling.
            result = DispatchResult(
                DispatchStatus.RETRYABLE,
                "connection_error",
                None,
                f"connection error dispatching to {event.target_service}: "
                f"{type(exc).__name__}",
            )
        else:
            result = self._classify(response.status_code, event.target_service)

        if self._metrics is not None:
            # Duration of the whole attempt (incl. timeouts), by target service.
            self._metrics.dispatch_duration_seconds.labels(
                event.target_service
            ).observe(time.perf_counter() - started)
        self._log(result, event)
        return result

    def _timeout_result(self, event: OutboxEvent) -> DispatchResult:
        detail = (
            f"dispatch to {event.target_service} exceeded {self._timeout_seconds:g}s"
        )
        return DispatchResult(DispatchStatus.RETRYABLE, "timeout", None, detail)

    @staticmethod
    def _classify(status_code: int, target_service: str) -> DispatchResult:
        if 200 <= status_code < 300:
            return DispatchResult(DispatchStatus.DELIVERED, "ok", status_code, "")
        detail = f"HTTP {status_code} from {target_service}"
        if status_code in RETRYABLE_STATUS_CODES:
            reason = "rate_limited" if status_code == 429 else "server_error"
            return DispatchResult(DispatchStatus.RETRYABLE, reason, status_code, detail)
        reason = PERMANENT_STATUS_REASONS.get(status_code, "unexpected_status")
        return DispatchResult(DispatchStatus.PERMANENT, reason, status_code, detail)

    @staticmethod
    def _log(result: DispatchResult, event: OutboxEvent) -> None:
        # correlation_id is already bound by the poller; never log the token or
        # response body. Level tracks severity: permanent failures are errors.
        fields = {
            "target_service": event.target_service,
            "event_type": event.event_type,
            "event_id": str(event.event_id),
            "status_code": result.status_code,
            "reason": result.reason,
        }
        if result.delivered:
            log.info("outbox.dispatch.delivered", **fields)
        elif result.retryable:
            log.warning("outbox.dispatch.retryable", **fields)
        else:
            log.error("outbox.dispatch.permanent", **fields)
