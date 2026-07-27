"""The bot command parser: a raw ``@radar`` mention becomes a validated BotCommand.

The one parse surface of stage 4. These are ordinary in-process tests (the parser is
pure — no DB, no Slack), but they carry the normal-plus care the surface warrants:
the closed verb set, unrecognised → help (bare vs unknown, distinct), and the
``incident <id>`` validated at parse.

Every case is driven against the RAW mention shape Slack delivers — ``<@U0BOT> status``,
handle included — never a pre-stripped string, so the handle normalisation (Finding 1)
is proven, not assumed: if the strip regressed, the verb would be ``<@U0BOT>`` and every
one of these would fall to UNKNOWN_COMMAND.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from radar_contracts import BotCommandType, BotMention
from radar_feedback_service.commands import (
    BotCommandError,
    BotParseFailure,
    parse_command,
)

USER_ID = "U0ENGINEER"
CHANNEL_ID = "C0FEEDBACK"
MESSAGE_TS = "1720000000.0001"
HANDLE = "<@U0BOT>"


def _mention(command_text: str) -> BotMention:
    """A BotMention as the plugin delivers it: the bot handle, then the command."""
    return BotMention(
        text=f"{HANDLE} {command_text}".strip(),
        user_id=USER_ID,
        channel_id=CHANNEL_ID,
        message_ts=MESSAGE_TS,
    )


def test_strips_the_handle_and_parses_the_verb() -> None:
    """Finding 1: the raw mention carries the bot handle; the parser normalises it away
    before matching, so `<@U0BOT> status` is STATUS (not the handle as a verb)."""
    cmd = parse_command(_mention("status"))
    assert cmd.command is BotCommandType.STATUS
    assert cmd.raw_text == "status"  # handle removed


def test_strips_the_piped_handle_form() -> None:
    """Slack may send `<@U0BOT|radar>`; the whole leading token is stripped."""
    mention = BotMention(
        text="<@U0BOT|radar> open",
        user_id=USER_ID,
        channel_id=CHANNEL_ID,
        message_ts=MESSAGE_TS,
    )
    assert parse_command(mention).command is BotCommandType.OPEN


def test_carries_the_routing_context() -> None:
    """The reply must land in-thread, so user/channel/thread ride through the parse."""
    cmd = parse_command(_mention("status"))
    assert cmd.slack_user_id == USER_ID
    assert cmd.channel == CHANNEL_ID
    assert cmd.thread_ref == MESSAGE_TS


def test_open_takes_no_args() -> None:
    assert parse_command(_mention("open")).command is BotCommandType.OPEN


def test_incident_validates_the_id_at_parse() -> None:
    incident_id = uuid4()
    cmd = parse_command(_mention(f"incident {incident_id}"))
    assert cmd.command is BotCommandType.INCIDENT
    assert cmd.incident_id == str(incident_id)


def test_incident_rejects_a_malformed_id_at_parse() -> None:
    """The one place stage 4 touches parse-boundary discipline: a bad id is rejected
    HERE, never passed to a lookup as a raw string. Distinct from 'no such incident'."""
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention("incident not-a-uuid"))
    assert exc.value.failure is BotParseFailure.BAD_ARGUMENT
    assert exc.value.given == "not-a-uuid"


def test_incident_requires_an_id() -> None:
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention("incident"))
    assert exc.value.failure is BotParseFailure.BAD_ARGUMENT


def test_last_parses_the_count() -> None:
    cmd = parse_command(_mention("last 5"))
    assert cmd.command is BotCommandType.LAST
    assert cmd.count == 5
    assert cmd.service is None


def test_last_parses_the_for_service_filter() -> None:
    cmd = parse_command(_mention("last 5 for order-service"))
    assert cmd.count == 5
    assert cmd.service == "order-service"


def test_last_rejects_a_non_numeric_count() -> None:
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention("last soon"))
    assert exc.value.failure is BotParseFailure.BAD_ARGUMENT


def test_last_rejects_a_non_positive_count() -> None:
    """count carries ge=1 on the contract; the parser rejects <1 up front with a
    message rather than letting the model raise a bare ValidationError."""
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention("last 0"))
    assert exc.value.failure is BotParseFailure.BAD_ARGUMENT


def test_last_for_without_a_service_is_rejected() -> None:
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention("last 5 for"))
    assert exc.value.failure is BotParseFailure.BAD_ARGUMENT


def test_summary_defaults_to_today() -> None:
    cmd = parse_command(_mention("summary"))
    assert cmd.command is BotCommandType.SUMMARY
    assert cmd.period == "today"


def test_summary_accepts_yesterday() -> None:
    assert parse_command(_mention("summary yesterday")).period == "yesterday"


def test_summary_rejects_an_unknown_period() -> None:
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention("summary lastweek"))
    assert exc.value.failure is BotParseFailure.BAD_ARGUMENT


def test_bare_mention_asks_for_help() -> None:
    """A mention with no command is EMPTY → the handler renders plain help, not silence
    and not a guess (Q1)."""
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention(""))
    assert exc.value.failure is BotParseFailure.EMPTY


def test_explicit_help_word_takes_the_help_path() -> None:
    """`@radar help` is the user asking what the bot does — plain help, not
    'unknown command: help'. (The small addition beyond the bare-vs-unknown split.)"""
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention("help"))
    assert exc.value.failure is BotParseFailure.EMPTY


def test_unknown_verb_is_named_distinctly_from_bare() -> None:
    """Q1's distinction: an unknown subcommand is UNKNOWN_COMMAND (so the reply can say
    'unknown command: reboot' + help), NOT the bare-mention EMPTY path."""
    with pytest.raises(BotCommandError) as exc:
        parse_command(_mention("reboot"))
    assert exc.value.failure is BotParseFailure.UNKNOWN_COMMAND
    assert exc.value.given == "reboot"
