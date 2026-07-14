"""What the worker puts on the wire: the envelope, and the target's own token.

**The body** is the ``EventEnvelope`` contract and nothing more. The outbox row and
the delivery envelope are different types on purpose: a row carries dispatch
bookkeeping — ``status``, ``attempts``, ``last_error``, ``process_after``, and its
database row ``id`` — that is the worker's private business. If any of it leaked
into the body, a consumer could come to depend on it, and the worker's internals
would have quietly become a public contract. The projection is pinned in both
directions, and the negative half is the one that matters: it is what a future
"just pass the whole row through, it's easier" refactor has to get past.

**The token** is the target's, not the worker's. Each agent validates against its
own token, so there is no single credential that opens them all — presenting the
wrong one is a 401, which the dispatcher classifies as *permanent*, so the event is
dead-lettered outright rather than retried. The fixture therefore holds a different
token per target: "sends the target's token" only means something when a wrong token
is available to send. A target with no token at all is refused before any request is
made, rather than dispatched unauthenticated.

The map's own guarantees are pinned at the bottom: it fails closed on an empty or
blank token, and neither its ``repr`` (which is logged at startup) nor its
config errors can expose a token value.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest
from radar_common import (
    AGENT_TOKEN_HEADER,
    REASONER_DISPATCH_TIMEOUT_SECONDS,
    REASONER_LLM_BUDGET_SECONDS,
    ConfigurationError,
)
from radar_contracts import EventEnvelope
from radar_database import OutboxEvent
from radar_outbox_worker.dispatcher import (
    DISPATCH_TIMEOUT_SECONDS,
    DispatchStatus,
    EventDispatcher,
    TargetResolver,
    TimeoutPolicy,
)
from radar_outbox_worker.main import OutboxWorkerSettings
from radar_outbox_worker.security import DispatchTokenMap, load_dispatch_tokens
from tokens import token_for

TARGET_SERVICE = "watcher-agent"
OTHER_SERVICE = "planner-agent"


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


def _row(target_service: str = TARGET_SERVICE) -> OutboxEvent:
    """An outbox row with every private field set to something recognizable.

    Not persisted: the dispatcher only reads attributes, and the point here is the
    row -> wire projection, not the database.
    """
    return OutboxEvent(
        id=uuid4(),
        event_id=uuid4(),
        event_type="alert.normalized",
        target_service=target_service,
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
    """A dispatcher holding a DIFFERENT token for each of two targets.

    Two entries, not one: "sends the target's token" is only a real assertion when
    there is a wrong token available to send.
    """
    return EventDispatcher(
        httpx.AsyncClient(transport=target),
        TargetResolver(
            overrides={
                TARGET_SERVICE: "http://watcher/events",
                OTHER_SERVICE: "http://planner/events",
            }
        ),
        DispatchTokenMap(
            {
                TARGET_SERVICE: token_for(TARGET_SERVICE),
                OTHER_SERVICE: token_for(OTHER_SERVICE),
            }
        ),
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


async def test_dispatch_sends_the_targets_own_token(
    dispatcher: EventDispatcher, target: CapturingTarget
) -> None:
    """Each target gets ITS token — not the worker's, and not another target's.

    Every agent validates against its own token, so presenting the wrong one is a
    401, which the dispatcher classifies as permanent: the event would be
    dead-lettered outright, not retried. Two dispatches to two targets, each
    checked against the token that target alone accepts.
    """
    await dispatcher.dispatch(_row(TARGET_SERVICE))
    await dispatcher.dispatch(_row(OTHER_SERVICE))

    sent = [r.headers[AGENT_TOKEN_HEADER] for r in target.requests]
    assert sent == [token_for(TARGET_SERVICE), token_for(OTHER_SERVICE)]
    # And the two are genuinely different, or the assertion above proves nothing.
    assert sent[0] != sent[1]


async def test_dispatch_to_unmapped_target_fails_closed(
    dispatcher: EventDispatcher, target: CapturingTarget
) -> None:
    """A target with no token is refused BEFORE any request is made.

    This is the fate of an event bound for a service that does not exist yet
    (Phase 9's feedback-service) or whose token was never minted. Dispatching it
    anyway would earn a 401 that says less about the cause — and would put an
    unauthenticated request on the wire, which is the thing worth not doing.
    """
    result = await dispatcher.dispatch(_row("feedback-service"))

    assert result.status is DispatchStatus.PERMANENT
    assert result.reason == "no_dispatch_token"
    assert result.status_code is None
    assert "feedback-service" in result.detail
    # The point: no request was made at all.
    assert target.requests == []


# --- the token map itself ----------------------------------------------------


def test_token_map_never_exposes_a_value_in_its_repr() -> None:
    """The map's repr shows target names only — it is logged at startup."""
    tokens = DispatchTokenMap({TARGET_SERVICE: token_for(TARGET_SERVICE)})

    rendered = f"{tokens!r} {tokens}"

    assert TARGET_SERVICE in rendered
    assert token_for(TARGET_SERVICE) not in rendered
    assert tokens.targets == [TARGET_SERVICE]


def test_token_map_rejects_an_empty_map() -> None:
    """A worker with no dispatch tokens can deliver nothing — fail at startup."""
    with pytest.raises(ConfigurationError):
        DispatchTokenMap({})


def test_token_map_rejects_an_empty_token() -> None:
    """A blank token would be sent as a blank header and 401 at the far end.

    Better to refuse at startup, where the message names the misconfigured target,
    than to dead-letter every event bound for it.
    """
    with pytest.raises(ConfigurationError) as exc:
        DispatchTokenMap({TARGET_SERVICE: ""})

    assert TARGET_SERVICE in str(exc.value)


def test_load_dispatch_tokens_reads_the_yaml_secret(tmp_path: Path) -> None:
    (tmp_path / "dispatch_tokens").write_text(
        f"{TARGET_SERVICE}: {token_for(TARGET_SERVICE)}\n"
        f"{OTHER_SERVICE}: {token_for(OTHER_SERVICE)}\n"
    )

    tokens = load_dispatch_tokens(directory=tmp_path)

    assert tokens.targets == sorted([TARGET_SERVICE, OTHER_SERVICE])
    watcher = tokens.get(TARGET_SERVICE)
    assert watcher is not None
    assert watcher.get_secret_value() == token_for(TARGET_SERVICE)


def test_load_dispatch_tokens_error_never_quotes_the_source(tmp_path: Path) -> None:
    """A YAML parse error quotes the offending line — which here is a token.

    So the raised message must carry the file's NAME and nothing from its contents.
    """
    secret = token_for(TARGET_SERVICE)
    (tmp_path / "dispatch_tokens").write_text(f"{TARGET_SERVICE}: [{secret}\n")

    with pytest.raises(ConfigurationError) as exc:
        load_dispatch_tokens(directory=tmp_path)

    assert secret not in str(exc.value)
    assert "dispatch_tokens" in str(exc.value)


def test_load_dispatch_tokens_rejects_a_non_mapping(tmp_path: Path) -> None:
    (tmp_path / "dispatch_tokens").write_text("- just\n- a list\n")

    with pytest.raises(ConfigurationError):
        load_dispatch_tokens(directory=tmp_path)


# --- per-target dispatch timeouts ---------------------------------------------


def test_the_timeout_is_resolved_per_target_from_config() -> None:
    """Config, not a special case in code.

    ``if target == "reasoner-agent": 90`` would be a rule nobody can change without
    a deploy and nobody can see in a ConfigMap — and it would have to be written
    again for the next slow target. The reasoner is not special; it is merely the
    first target that calls an LLM before it can answer.
    """
    policy = TimeoutPolicy(overrides={"reasoner-agent": 90.0})

    assert policy.resolve("reasoner-agent") == 90.0
    assert policy.resolve("watcher-agent") == DISPATCH_TIMEOUT_SECONDS
    assert policy.resolve("a-service-nobody-configured") == DISPATCH_TIMEOUT_SECONDS


def test_the_worker_waits_longer_than_the_reasoner_takes() -> None:
    """THE invariant. Both numbers live in radar_common, ordered against each other.

    If the worker's timeout were the shorter of the two, it would give up while the
    reasoner is still talking to OpenAI, classify the dispatch retryable, and
    redeliver — and the redelivery would start a SECOND LLM call, because the first
    transaction has not committed its processed_events marker yet. The platform pays
    twice and two recommendations race for one incident.

    The idempotency gate cannot prevent that: it guards redelivery AFTER a commit,
    not during an in-flight call. Only the order of these two numbers does.
    """
    assert REASONER_DISPATCH_TIMEOUT_SECONDS > REASONER_LLM_BUDGET_SECONDS

    # And the worker's shipped default really does use it — a deployment that forgets
    # to set the override still gets a reasoner that is not cut off mid-call.
    settings = OutboxWorkerSettings()
    policy = TimeoutPolicy(overrides=settings.dispatch_timeout_overrides)
    assert policy.resolve("reasoner-agent") == REASONER_DISPATCH_TIMEOUT_SECONDS
    assert policy.resolve("reasoner-agent") > REASONER_LLM_BUDGET_SECONDS


@pytest.mark.parametrize("bad", [0.0, -1.0])
def test_a_nonpositive_timeout_is_refused(bad: float) -> None:
    """A zero timeout would fail every dispatch instantly. Refuse at startup."""
    with pytest.raises(ConfigurationError):
        TimeoutPolicy(overrides={"reasoner-agent": bad})
    with pytest.raises(ConfigurationError):
        TimeoutPolicy(default_seconds=bad)


async def test_a_slow_target_times_out_at_its_own_budget_not_the_default(
    target: CapturingTarget,
) -> None:
    """A target with a long budget is not cut off at the 10s default.

    Driven against a transport that never answers, with the clock compressed: the
    default would fire at 0.05s and the override at 0.3s. The dispatch must survive
    past the default, which is the whole point of the override existing.
    """
    slow = _NeverAnswers()
    dispatcher = EventDispatcher(
        httpx.AsyncClient(transport=slow),
        TargetResolver(overrides={"reasoner-agent": "http://reasoner/events"}),
        DispatchTokenMap({"reasoner-agent": token_for("reasoner-agent")}),
        timeouts=TimeoutPolicy(default_seconds=0.05, overrides={"reasoner-agent": 0.3}),
    )

    started = time.perf_counter()
    result = await dispatcher.dispatch(_row("reasoner-agent"))
    elapsed = time.perf_counter() - started

    assert result.retryable
    assert result.reason == "timeout"
    assert elapsed > 0.05 * 2, (
        "the dispatch was cut off at the DEFAULT timeout — the per-target override "
        "was not applied, and a real reasoner would be killed mid-LLM-call"
    )
    assert "0.3s" in result.detail, "the error names the budget that actually applied"


class _NeverAnswers(httpx.AsyncBaseTransport):
    """A target that accepts the connection and never responds."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(3600)
        raise AssertionError("unreachable")
