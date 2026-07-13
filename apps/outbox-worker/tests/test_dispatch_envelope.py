"""The dispatch wire body is the EventEnvelope contract, and nothing more.

The outbox row and the delivery envelope are different types on purpose. A row
carries dispatch bookkeeping — ``status``, ``attempts``, ``last_error``,
``process_after``, and its database row ``id`` — that is the worker's private
business. If any of it leaked into the body, a consumer could come to depend on
it, and the worker's internals would have quietly become a public contract.

These pin the projection in both directions: the four contract fields are present
and carry the row's values, and nothing else is. The negative half is the one that
matters — it is what a future "just pass the whole row through, it's easier"
refactor has to get past.
"""

from __future__ import annotations

import json
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from pydantic import SecretStr
from radar_common import AGENT_TOKEN_HEADER
from radar_contracts import EventEnvelope
from radar_database import OutboxEvent
from radar_outbox_worker.dispatcher import EventDispatcher, TargetResolver

TARGET_SERVICE = "watcher-agent"
AGENT_TOKEN = "a" * 64


class CapturingTarget(httpx.AsyncBaseTransport):
    """Accepts any dispatch and records the request it received."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return httpx.Response(200)

    @property
    def body(self) -> dict[str, Any]:
        assert len(self.requests) == 1
        parsed: dict[str, Any] = json.loads(self.requests[0].content)
        return parsed


def _row() -> OutboxEvent:
    """An outbox row with every private field set to something recognizable.

    Not persisted: the dispatcher only reads attributes, and the point here is the
    row -> wire projection, not the database.
    """
    return OutboxEvent(
        id=uuid4(),
        event_id=uuid4(),
        event_type="alert.normalized",
        target_service=TARGET_SERVICE,
        payload={"service_name": "order-service", "deduplicated": False},
        correlation_id=uuid4(),
        status="processing",
        attempts=3,
        last_error="HTTP 503 from watcher-agent",
    )


@pytest.fixture
def target() -> CapturingTarget:
    return CapturingTarget()


@pytest.fixture
def dispatcher(target: CapturingTarget) -> EventDispatcher:
    return EventDispatcher(
        httpx.AsyncClient(transport=target),
        TargetResolver(overrides={TARGET_SERVICE: "http://watcher/events"}),
        SecretStr(AGENT_TOKEN),
    )


async def test_wire_body_carries_the_four_contract_fields(
    dispatcher: EventDispatcher, target: CapturingTarget
) -> None:
    event = _row()

    result = await dispatcher.dispatch(event)

    assert result.delivered
    body = target.body
    assert UUID(body["event_id"]) == event.event_id
    assert body["event_type"] == event.event_type
    assert UUID(body["correlation_id"]) == event.correlation_id
    assert body["payload"] == event.payload
    # And it round-trips back through the contract the receiving agent parses —
    # producer and consumer share one model, so drift is a type error, not a 422.
    assert EventEnvelope.model_validate(body).event_id == event.event_id


async def test_wire_body_leaks_no_row_bookkeeping(
    dispatcher: EventDispatcher, target: CapturingTarget
) -> None:
    """The row's dispatch state must not cross the wire — not even the row id."""
    event = _row()

    await dispatcher.dispatch(event)

    body = target.body
    assert set(body) == {"event_id", "event_type", "correlation_id", "payload"}
    # Spelled out, because each one is a distinct way to leak: the row key that
    # would let a consumer address the outbox directly, and the retry state that
    # would let it branch on how badly delivery has been going.
    for private in ("id", "status", "attempts", "last_error", "process_after"):
        assert private not in body
    # The row id is a different UUID from the event id — a consumer recording the
    # wrong one in processed_events would break idempotency across a redelivery,
    # since the worker re-sends the same event_id but the row id is stable too.
    assert UUID(body["event_id"]) != event.id


async def test_dispatch_sends_the_agent_token(
    dispatcher: EventDispatcher, target: CapturingTarget
) -> None:
    await dispatcher.dispatch(_row())

    assert target.requests[0].headers[AGENT_TOKEN_HEADER] == AGENT_TOKEN
