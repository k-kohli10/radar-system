"""The ``@radar`` bot: parse a mention, run the read command, reply in-thread.

The app-side handler the Socket Mode source dispatches each mention to. It ties the
stage-4 pieces together: :func:`~radar_feedback_service.commands.parse_command` turns
the raw mention into a :class:`BotCommand`, a repository query answers it, a formatter
renders the reply, and it is posted as a threaded reply under the mention.

Two things this layer owns:

- **Every mention gets a reply, an unparseable one included.** A bare mention, an
  unknown verb, or a bad argument raises :class:`BotCommandError` from the parser; this
  handler renders the matching help/error text and posts it. Swallowing the raise into
  a silent no-op would undo the parser and leave a user who typed something wrong
  staring at nothing.
- **The cap bites here.** The parser accepts any ``last <n>`` with n≥1; policy is this
  layer's, so ``last`` clamps the count to ``max_rows`` (the deployment's
  ``bot_max_rows``) before it reaches the query's ``LIMIT``. The repository bounds by
  the argument; the handler decides the argument.

Read-only: every command is a SELECT-and-render, and nothing here mutates state.
Replies are posted in-thread (``thread_ref`` = the mention's ts) so an answer sits
under its question and the channel stays a clean incident feed.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from uuid import UUID

from radar_common import get_logger, utcnow
from radar_contracts import (
    BotCommand,
    BotCommandType,
    BotMention,
    BotResponse,
    NotificationBackend,
)
from radar_database import (
    Database,
    FeedbackRepository,
    Incident,
    IncidentRepository,
    OutboxEventRepository,
    Recommendation,
    RecommendationRepository,
)
from sqlalchemy.ext.asyncio import AsyncSession

from radar_feedback_service.commands import (
    BotCommandError,
    BotParseFailure,
    parse_command,
)
from radar_feedback_service.interactions import MentionHandler

log = get_logger("feedback.bot")

_HELP = (
    "I can help with:\n"
    "• `@radar status` — open incident count, last RCA, outbox depth\n"
    "• `@radar open` — the currently open incidents\n"
    "• `@radar incident <id>` — details for one incident\n"
    "• `@radar last <n> [for <service>]` — the most recent incidents\n"
    "• `@radar summary [today|yesterday]` — the day's incident summary"
)


def build_mention_handler(
    database: Database, notifier: NotificationBackend, *, max_rows: int
) -> MentionHandler:
    """The adapter the wired source dispatches each ``@radar`` mention to.

    Parses the mention, runs the command against ``database``, and posts the reply
    in-thread via ``notifier``. An unparseable mention becomes a help/error reply rather
    than a silent drop (see the module docstring). ``max_rows`` is the deployment's cap,
    applied to the row-returning commands.
    """

    async def handle(mention: BotMention) -> None:
        try:
            command = parse_command(mention)
        except BotCommandError as exc:
            # Render the matching help/error and reply. NEVER a silent return: a
            # rejected mention with no reply leaves the user with nothing.
            log.info(
                "bot.command_rejected",
                failure=exc.failure.value,
                user_id=mention.user_id,
            )
            await _reply(notifier, mention, _rejection_reply(exc))
            return
        response = await _run_command(command, database, max_rows=max_rows)
        log.info(
            "bot.command_handled",
            command=command.command.value,
            user_id=mention.user_id,
        )
        await _reply(notifier, mention, response)

    return handle


async def _run_command(
    command: BotCommand, database: Database, *, max_rows: int
) -> BotResponse:
    async with database.session() as session:
        return await _RUNNERS[command.command](command, session, max_rows)


async def _reply(
    notifier: NotificationBackend, mention: BotMention, response: BotResponse
) -> None:
    """Post the reply in-thread, best-effort. The mention is already acked (no
    redelivery), so a failed post is logged and dropped: nothing to retry."""
    try:
        await notifier.send(
            mention.channel_id,
            response.text,
            blocks=response.blocks,
            thread_ref=mention.message_ts,
        )
    except Exception as exc:
        log.warning(
            "bot.reply_failed",
            channel_id=mention.channel_id,
            error=type(exc).__name__,
        )


def _rejection_reply(exc: BotCommandError) -> BotResponse:
    """Render a parse rejection: plain help (bare/help), unknown-command + help, or the
    specific bad-argument message + help."""
    if exc.failure is BotParseFailure.EMPTY:
        return BotResponse(text=_HELP)
    if exc.failure is BotParseFailure.UNKNOWN_COMMAND:
        return BotResponse(text=f"Unknown command: `{exc.given}`\n\n{_HELP}")
    return BotResponse(text=f"{exc}\n\n{_HELP}")


async def _run_status(
    command: BotCommand, session: AsyncSession, _max_rows: int
) -> BotResponse:
    active = await IncidentRepository(session).count_active()
    last_rca = await RecommendationRepository(session).latest_created_at()
    depth = await OutboxEventRepository(session).count_pending()
    last = _ago(last_rca, utcnow()) if last_rca is not None else "none yet"
    return BotResponse(
        text=(
            "*RADAR status*\n"
            f"• Open incidents: {active}\n"
            f"• Last RCA: {last}\n"
            f"• Outbox depth: {depth}"
        )
    )


async def _run_open(
    command: BotCommand, session: AsyncSession, max_rows: int
) -> BotResponse:
    rows = await IncidentRepository(session).list_active(limit=max_rows)
    if not rows:
        return BotResponse(text="No open incidents. :tada:")
    lines = [f"*{len(rows)} open incident(s)*", *(_incident_line(i) for i in rows)]
    return BotResponse(text="\n".join(lines))


async def _run_incident(
    command: BotCommand, session: AsyncSession, _max_rows: int
) -> BotResponse:
    # incident_id is parser-validated to a UUID string for the INCIDENT verb.
    assert command.incident_id is not None
    incident = await IncidentRepository(session).get(UUID(command.incident_id))
    if incident is None:
        # A valid id with no row: distinct from the parser's "not a valid id".
        return BotResponse(text=f"No such incident: `{command.incident_id}`")
    rec = await RecommendationRepository(session).latest_for_incident(incident.id)
    return BotResponse(text=_incident_detail(incident, rec))


async def _run_last(
    command: BotCommand, session: AsyncSession, max_rows: int
) -> BotResponse:
    # THE CLAMP: the parser accepted any n≥1; policy caps it here before the query.
    assert command.count is not None
    limit = min(command.count, max_rows)
    incidents = IncidentRepository(session)
    rows = await incidents.recent(limit=limit, service=command.service)
    scope = f" for `{command.service}`" if command.service else ""
    if not rows:
        return BotResponse(text=f"No incidents{scope}.")
    header = f"*Last {len(rows)} incident(s){scope}*"
    return BotResponse(text="\n".join([header, *(_incident_line(i) for i in rows)]))


async def _run_summary(
    command: BotCommand, session: AsyncSession, _max_rows: int
) -> BotResponse:
    start, end, label = _window(command.period or "today", utcnow())
    incidents = IncidentRepository(session)
    opened = await incidents.count_opened_between(start, end)
    resolved = await incidents.count_resolved_between(start, end)
    feedback = await FeedbackRepository(session).count_by_sentiment_between(start, end)
    return BotResponse(
        text=(
            f"*RADAR summary — {label}*\n"
            f"• Opened: {opened}\n"
            f"• Resolved: {resolved}\n"
            f"• Feedback: {feedback.get('helpful', 0)} :+1: / "
            f"{feedback.get('not_helpful', 0)} :-1:"
        )
    )


_RUNNERS: dict[
    BotCommandType,
    Callable[[BotCommand, AsyncSession, int], Awaitable[BotResponse]],
] = {
    BotCommandType.STATUS: _run_status,
    BotCommandType.OPEN: _run_open,
    BotCommandType.INCIDENT: _run_incident,
    BotCommandType.LAST: _run_last,
    BotCommandType.SUMMARY: _run_summary,
}


def _incident_line(incident: Incident) -> str:
    return (
        f"• `{incident.id}` — {incident.service_name} / {incident.severity} / "
        f"{incident.status} — {incident.title}"
    )


def _incident_detail(incident: Incident, rec: Recommendation | None) -> str:
    lines = [
        f"*Incident* `{incident.id}`",
        f"• Service: {incident.service_name}",
        f"• Severity: {incident.severity}",
        f"• Status: {incident.status}",
        f"• Title: {incident.title}",
    ]
    lines.append(f"• Root cause: {rec.root_cause}" if rec else "• No RCA yet.")
    return "\n".join(lines)


def _ago(then: datetime, now: datetime) -> str:
    """A compact relative time: "4m ago", "2h ago", "3d ago", or "just now"."""
    seconds = int((now - then).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def _window(period: str, now: datetime) -> tuple[datetime, datetime, str]:
    """The half-open ``[start, end)`` for ``today``/``yesterday`` in UTC, the same
    convention the count queries use, so the two days partition without overlap."""
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if period == "yesterday":
        return today - timedelta(days=1), today, "yesterday"
    return today, today + timedelta(days=1), "today"
