"""Parse a raw ``@radar`` mention into a validated :class:`BotCommand`. Pure.

The one parse surface of the Slack bot: where untrusted free text becomes a closed,
typed command. Everything downstream is a SELECT-and-render. Two discipline points:

- **The bot handle is normalised away FIRST.** Slack delivers the mention text exactly
  as typed (``<@U0BOT> status``), so the leading ``<@…>`` token is stripped before any
  verb matching. Parse against the RAW mention, never a pre-stripped string, or every
  command fails to match: the plugin hands over what Slack sent, and normalising it is
  this module's job.
- **A malformed ``incident <id>`` is rejected AT PARSE**, never passed to a lookup as a
  raw string. A bad id ("that's not a valid incident id") and a valid-but-absent id
  ("no such incident", decided at query time downstream) tell the engineer different
  things and must stay distinct.

Unrecognised input is never silence and never a guess: a bare mention asks for help, an
unknown verb says so and then helps, and a bad argument says what was wrong. The parser
signals which case via :class:`BotParseFailure` on a raised :class:`BotCommandError`,
and the handler renders the matching reply. Raising rather than returning a sentinel
keeps an unparseable mention from looking handled.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import StrEnum
from uuid import UUID

from radar_common import RadarError
from radar_contracts import BotCommand, BotCommandType, BotMention

#: A leading Slack mention token: ``<@U0BOT>`` or the piped ``<@U0BOT|radar>`` form.
#: Only the FIRST is stripped: it is the bot handle that triggered the app_mention.
#: Any ``<@…>`` later in the text is an argument the user typed and is left alone.
_MENTION_RE = re.compile(r"^\s*<@[^>]+>\s*")

_FOR = "for"

#: Typed as help, not as a command: an explicit ``@radar help`` (or ``?``) is the user
#: asking what the bot does, so it takes the same plain-help path as a bare mention
#: rather than answering "unknown command: help".
_HELP_WORDS = frozenset({"help", "?"})

_SUMMARY_PERIODS = frozenset({"today", "yesterday"})
_DEFAULT_PERIOD = "today"


class BotParseFailure(StrEnum):
    """Why a mention is not a runnable command; the handler renders one reply per kind.

    ``EMPTY`` → plain help (a bare mention or an explicit ``help``). ``UNKNOWN_COMMAND``
    → "unknown command: X" plus help. ``BAD_ARGUMENT`` → the specific message (a bad
    ``<id>``, a non-numeric ``last``, an unknown ``summary`` period).
    """

    EMPTY = "empty"
    UNKNOWN_COMMAND = "unknown_command"
    BAD_ARGUMENT = "bad_argument"


class BotCommandError(RadarError):
    """A mention that is not a runnable command. Carries ``failure`` so the handler can
    render the right reply, and ``given`` (the offending token) where there is one.

    Raised rather than returned as a sentinel: an unparseable mention must produce a
    visible reply, never a silent drop that looks like it was handled.
    """

    def __init__(
        self, failure: BotParseFailure, message: str, *, given: str | None = None
    ) -> None:
        self.failure = failure
        self.given = given
        super().__init__(message)


def parse_command(mention: BotMention) -> BotCommand:
    """Parse ``mention`` into a validated :class:`BotCommand`, or raise.

    Strips the leading bot handle, matches the verb against the closed
    :class:`BotCommandType` set, and validates that verb's arguments. Raises
    :class:`BotCommandError` for a bare mention (help), an unknown verb, or a bad
    argument. The routing context (user/channel/thread) is carried through so the
    handler can reply in-thread.
    """
    body = _strip_handle(mention.text)
    tokens = body.split()
    if not tokens or tokens[0].lower() in _HELP_WORDS:
        raise BotCommandError(BotParseFailure.EMPTY, "no runnable command")

    verb, args = tokens[0], tokens[1:]
    try:
        command = BotCommandType(verb.lower())
    except ValueError as exc:
        raise BotCommandError(
            BotParseFailure.UNKNOWN_COMMAND,
            f"unknown command: {verb}",
            given=verb,
        ) from exc

    return BotCommand(
        command=command,
        raw_text=body,
        slack_user_id=mention.user_id,
        channel=mention.channel_id,
        thread_ref=mention.message_ts,
        **_ARG_PARSERS[command](args),
    )


def _strip_handle(text: str) -> str:
    """Remove the leading ``<@…>`` bot handle and surrounding whitespace.

    The mention arrives as ``<@U0BOT> status``, so without this the first token would
    be the handle and no verb would ever match.
    """
    return _MENTION_RE.sub("", text, count=1).strip()


def _no_args(_args: list[str]) -> dict[str, object]:
    """``status`` / ``open`` take no arguments; any trailing text is ignored."""
    return {}


def _incident_args(args: list[str]) -> dict[str, object]:
    if not args:
        raise BotCommandError(
            BotParseFailure.BAD_ARGUMENT, "usage: `@radar incident <id>`"
        )
    raw = args[0]
    try:
        # Validate at PARSE, never hand a raw string to a lookup. Stored normalised so
        # the handler queries a real id; a missing row is a DIFFERENT reply downstream.
        incident_id = str(UUID(raw))
    except ValueError as exc:
        raise BotCommandError(
            BotParseFailure.BAD_ARGUMENT,
            f"not a valid incident id: {raw}",
            given=raw,
        ) from exc
    return {"incident_id": incident_id}


def _last_args(args: list[str]) -> dict[str, object]:
    if not args:
        raise BotCommandError(
            BotParseFailure.BAD_ARGUMENT, "usage: `@radar last <n> [for <service>]`"
        )
    try:
        count = int(args[0])
    except ValueError as exc:
        raise BotCommandError(
            BotParseFailure.BAD_ARGUMENT,
            f"`last` needs a number, got: {args[0]}",
            given=args[0],
        ) from exc
    if count < 1:
        raise BotCommandError(
            BotParseFailure.BAD_ARGUMENT, "`last` needs a positive number"
        )
    return {"count": count, "service": _parse_for(args[1:])}


def _parse_for(rest: list[str]) -> str | None:
    """The optional ``for <service>`` tail of ``last``. Absent means no filter."""
    if not rest:
        return None
    if rest[0].lower() != _FOR or len(rest) < 2:
        raise BotCommandError(
            BotParseFailure.BAD_ARGUMENT,
            f"expected `for <service>`, got: {' '.join(rest)}",
        )
    return rest[1]


def _summary_args(args: list[str]) -> dict[str, object]:
    if not args:
        return {"period": _DEFAULT_PERIOD}
    period = args[0].lower()
    if period not in _SUMMARY_PERIODS:
        raise BotCommandError(
            BotParseFailure.BAD_ARGUMENT,
            f"`summary` period must be today or yesterday, got: {args[0]}",
            given=args[0],
        )
    return {"period": period}


_ARG_PARSERS: dict[BotCommandType, Callable[[list[str]], dict[str, object]]] = {
    BotCommandType.STATUS: _no_args,
    BotCommandType.OPEN: _no_args,
    BotCommandType.INCIDENT: _incident_args,
    BotCommandType.LAST: _last_args,
    BotCommandType.SUMMARY: _summary_args,
}
