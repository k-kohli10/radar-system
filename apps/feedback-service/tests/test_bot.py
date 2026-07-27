"""The @radar bot handler: a mention becomes a query and an in-thread reply.

Ordinary read-path tests against real Postgres — each command queries and renders the
right reply. No mutation-proving a SELECT. Two behaviours DO earn a hard look, and both
are driven through the WIRED path (the plugin's ack_and_dispatch into the real handler),
not the handler in isolation:

- **Every mention gets a reply — an unparseable one too.** A bad mention driven through
  the listener must SURFACE its help/error reply, never a swallowed no-op. Same reason
  the callback reject had to be proven through the wired listener: a handler that
  swallowed BotCommandError would silently undo the parser. Proven by mutation.
- **The cap bites at the handler.** `@radar last 1000` returns bot_max_rows rows, not a
  thousand — the parser accepted n≥1, the handler clamps to policy before the query.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock
from uuid import uuid4

from fakes import FakeNotifier
from radar_contracts import BotMention, NotificationInteraction
from radar_database import Database, Incident
from radar_feedback_service.bot import build_mention_handler
from radar_plugin_notifications_slack import ack_and_dispatch
from slack_sdk.socket_mode.request import SocketModeRequest

USER = "U0ENGINEER"
CHANNEL = "C0FEEDBACK"
TS = "1720000000.0001"
HANDLE = "<@U0BOT>"


def _mention(command_text: str) -> BotMention:
    return BotMention(
        text=f"{HANDLE} {command_text}".strip(),
        user_id=USER,
        channel_id=CHANNEL,
        message_ts=TS,
    )


def _incident(*, status: str = "open", service: str = "order-service") -> Incident:
    return Incident(
        id=uuid4(),
        correlation_id=uuid4(),
        fingerprint="f" * 64,
        service_name=service,
        title="Orders failing",
        severity="high",
        status=status,
    )


async def _seed(db: Database, *rows: Incident) -> None:
    async with db.session() as session:
        session.add_all(rows)
        await session.commit()


def _last_reply(notifier: FakeNotifier) -> dict[str, Any]:
    assert notifier.calls, "no reply was posted"
    return notifier.calls[-1]


async def _interaction_must_not_fire(interaction: NotificationInteraction) -> None:
    raise AssertionError("interaction handler must not see a mention")


async def _drive(
    db: Database, notifier: FakeNotifier, text: str, *, max_rows: int
) -> None:
    """Push a mention through the wired path: ack_and_dispatch -> the real handler."""
    handler = build_mention_handler(db, notifier, max_rows=max_rows)
    payload = {
        "type": "event_callback",
        "event": {
            "type": "app_mention",
            "text": f"{HANDLE} {text}".strip(),
            "user": USER,
            "channel": CHANNEL,
            "ts": TS,
        },
    }
    request = SocketModeRequest(type="events_api", envelope_id="e", payload=payload)
    await ack_and_dispatch(AsyncMock(), request, _interaction_must_not_fire, handler)


# --- each command queries and renders ----------------------------------------------


async def test_status_reports_counts_in_thread(db: Database) -> None:
    await _seed(db, _incident(status="open"), _incident(status="investigating"))
    notifier = FakeNotifier()
    await build_mention_handler(db, notifier, max_rows=20)(_mention("status"))
    reply = _last_reply(notifier)
    assert "Open incidents: 2" in reply["text"]
    assert reply["channel"] == CHANNEL
    assert reply["thread_ref"] == TS  # reply is threaded under the mention


async def test_open_lists_live_incidents(db: Database) -> None:
    await _seed(db, _incident(status="open"), _incident(status="resolved"))
    notifier = FakeNotifier()
    await build_mention_handler(db, notifier, max_rows=20)(_mention("open"))
    assert "1 open incident" in _last_reply(notifier)["text"]  # resolved excluded


async def test_open_with_none_says_so(db: Database) -> None:
    notifier = FakeNotifier()
    await build_mention_handler(db, notifier, max_rows=20)(_mention("open"))
    assert "No open incidents" in _last_reply(notifier)["text"]


async def test_incident_detail_shows_the_incident(db: Database) -> None:
    incident = _incident()
    await _seed(db, incident)
    notifier = FakeNotifier()
    await build_mention_handler(db, notifier, max_rows=20)(
        _mention(f"incident {incident.id}")
    )
    assert str(incident.id) in _last_reply(notifier)["text"]


async def test_incident_missing_is_distinct_from_malformed(db: Database) -> None:
    """A valid id with no row -> 'No such incident' (the query-time miss), distinct from
    the parser's 'not a valid id' for a malformed one."""
    notifier = FakeNotifier()
    await build_mention_handler(db, notifier, max_rows=20)(
        _mention(f"incident {uuid4()}")
    )
    assert "No such incident" in _last_reply(notifier)["text"]


async def test_summary_renders(db: Database) -> None:
    notifier = FakeNotifier()
    await build_mention_handler(db, notifier, max_rows=20)(_mention("summary today"))
    assert "RADAR summary" in _last_reply(notifier)["text"]


# --- the clamp bites at the handler ------------------------------------------------


async def test_last_clamps_the_count_to_max_rows(db: Database) -> None:
    """`@radar last 1000` with a cap of 20 returns 20 rows, not the 25 that exist and
    not 1000 — the parser accepted the big number, the handler clamped it to policy
    before the query. Drop the clamp (pass command.count straight through) and this
    returns all 25, turning red: the bounded-authority guarantee at the handler layer.
    """
    await _seed(db, *(_incident(status="open") for _ in range(25)))
    notifier = FakeNotifier()
    await build_mention_handler(db, notifier, max_rows=20)(_mention("last 1000"))
    text = _last_reply(notifier)["text"]
    assert text.count("• `") == 20  # one bullet per incident line
    assert "Last 20 incident" in text


# --- the WIRED path: replies surface, happy and unhappy ----------------------------


async def test_open_through_the_listener_lists_incidents(db: Database) -> None:
    """The phase's done-condition: an @radar open mention driven through the wired
    listener queries Postgres and replies with the open incidents."""
    await _seed(db, _incident(status="open"), _incident(status="open"))
    notifier = FakeNotifier()
    await _drive(db, notifier, "open", max_rows=20)
    assert "2 open incident" in _last_reply(notifier)["text"]


async def test_bad_mention_through_the_listener_surfaces_a_reply(db: Database) -> None:
    """The carry-forward: an unparseable mention driven through the listener SURFACES
    its reply (help / unknown-command / bad-argument), never a swallowed no-op.

    MUTATION guard: make the handler swallow BotCommandError (reply on the error path
    removed) and every one of these goes red — the swallow this test forbids, the same
    silent-undo-the-parser failure the callback listener test guards against.
    """
    cases = [
        ("", "I can help with"),  # bare -> plain help
        ("reboot", "Unknown command"),  # unknown verb + help
        ("incident not-a-uuid", "not a valid incident id"),  # bad arg, at parse
    ]
    for text, expected in cases:
        notifier = FakeNotifier()
        await _drive(db, notifier, text, max_rows=20)
        reply = _last_reply(notifier)  # asserts a reply was posted at all
        assert expected in reply["text"], text
        assert reply["thread_ref"] == TS
