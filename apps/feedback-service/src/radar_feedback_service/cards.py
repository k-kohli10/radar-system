"""The RCA card: a recommendation and its incident, rendered to Slack blocks.

A pure function. It takes the fields the caller has already read from the
``recommendations`` and ``incidents`` rows and returns a Block Kit ``blocks`` list
plus the fallback ``text``. No database, no Slack client, no I/O — so it is
trivially testable and the delivery path (which does the I/O) stays thin.

Rendered from ROWS, never from the event payload: an incident keeps moving after
the RCA is written (severity escalates, the incident resolves) and a
recommendation is the one row a human can later correct, so the card must show
what the incident IS now, not a frozen copy from when the event was emitted. The
caller passes current values; this function only lays them out.

Two variants, keyed on ``is_fallback``:

- **AI analysis** — the reasoner's LLM produced the RCA.
- **AI Unavailable** — the gateway was down, so the RCA is the planner's own
  investigation steps (``is_fallback=True``, ``confidence=low``). The header says
  so plainly: an engineer must not read a template checklist as a model's
  diagnosis. The row carries the distinction; the card surfaces it.

The interactive controls (👍 / 👎 / Resolve) live in the actions block. Each button
carries a RADAR ``action_id`` from the closed :class:`InteractionAction` set — the SAME
enum the callback parser reads — and the recommendation id as its ``value``. So the
formatter that WRITES a button and the parser that READS the click back share one
definition and cannot drift; this is the send half of that contract. The buttons landed
with their handler (not before): a button Slack renders but nothing handles would fail
on click, so the click path — parser, handler, and the wired Socket Mode listener —
exists before these are shown.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from radar_contracts import RecommendedAction

from radar_feedback_service.callbacks import InteractionAction

#: Slack hard-limits a section's text at 3000 chars and rejects the whole message
#: if any block exceeds it. A long root cause is a delivery failure, not a
#: cosmetic overflow, so oversized fields are truncated to fit with a marker. Kept
#: below 3000 to leave room for the surrounding markdown.
_SECTION_TEXT_LIMIT = 2900

_TRUNCATION_MARKER = "… (truncated)"

#: Mirrors ``radar_database.lifecycle.STATUS_RESOLVED``. Not imported directly — this
#: module is deliberately database-free (see the module docstring) — but the two must
#: agree, since this is what decides whether the Resolve button still renders.
_STATUS_RESOLVED = "resolved"

#: Severity is ingestion's vocabulary (``radar_ingestion.normalizer``), not this
#: module's — a value outside this set is a contract slip upstream, so it falls back
#: to a neutral dot rather than raising: the card must still deliver (same "visible,
#: not silent" rule as ``_format_actions``'s empty-list placeholder).
_SEVERITY_ICON = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🟢",
}

#: Confidence is the reasoner's self-rating of its own RCA — inverted from severity
#: (low confidence is the warning sign here), so it gets its own map rather than
#: reusing ``_SEVERITY_ICON`` by accident.
_CONFIDENCE_ICON = {
    "high": "🟢",
    "medium": "🟡",
    "low": "🔴",
}

_UNKNOWN_ICON = "⚪"


@dataclass(frozen=True)
class RcaCardData:
    """Exactly what the card needs, read from the two rows by the caller.

    Plain values, not ORM models: the formatter stays decoupled from the database
    and a test builds one in a line. ``severity`` and ``confidence`` are the
    stored strings (the card displays them; it does not reason over them).
    """

    incident_id: UUID
    recommendation_id: UUID
    service_name: str
    title: str
    severity: str
    status: str
    root_cause: str
    confidence: str
    recommended_actions: Sequence[RecommendedAction]
    is_fallback: bool


def format_rca_card(
    data: RcaCardData, *, ack: str | None = None
) -> tuple[str, list[dict[str, object]]]:
    """Render ``data`` to ``(fallback_text, blocks)`` for a Slack notification.

    ``fallback_text`` is the notification/preview string Slack shows where blocks
    do not render (push notifications, the sidebar) — always meaningful, never
    empty. ``blocks`` is the card layout.

    ``ack`` appends one context line acknowledging an interaction (a 👍/👎 recorded,
    or the incident resolved) when the callback handler re-renders the card in place.
    It is the ONLY addition the reflection makes: the rest of the card is rebuilt
    from the current rows, so a resolved incident already shows ``Status: resolved``
    without the footer having to say it twice. ``None`` (the delivery path) renders
    the plain card with no acknowledgement line.
    """
    header = _header_text(data)
    fallback_text = f"{header}: {data.title}"

    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {
            "type": "section",
            "fields": [
                _field("Service", data.service_name),
                _field("Severity", _iconize(data.severity, _SEVERITY_ICON)),
                _field("Status", data.status.capitalize()),
                _field("Confidence", _iconize(data.confidence, _CONFIDENCE_ICON)),
            ],
        },
        {"type": "divider"},
        _section(f"*Root cause*\n{data.root_cause}"),
        _section(f"*Recommended actions*\n{_format_actions(data.recommended_actions)}"),
        _actions_block(
            data.recommendation_id, resolved=data.status == _STATUS_RESOLVED
        ),
    ]

    if data.status == _STATUS_RESOLVED:
        # Block Kit buttons have no disabled state — a "greyed out" Resolve button is
        # not something Slack offers. The honest equivalent: the button is gone (there
        # is nothing left to resolve), and this static line says so where it was.
        blocks.append(_context("✅ *Resolved* — no further action needed"))

    if data.is_fallback:
        # The engineer is reading the planner's investigation steps, not a model's
        # diagnosis. Say it again at the foot, so the distinction survives a card
        # skimmed from the bottom up.
        blocks.append(
            _context(
                "⚠️ Generated without AI — the LLM gateway was unavailable, so these "
                "are RADAR's standard investigation steps for this alert."
            )
        )

    blocks.append(_context(f"Incident `{data.incident_id}`"))
    if ack is not None:
        blocks.append(_context(ack))
    return fallback_text, blocks


def _header_text(data: RcaCardData) -> str:
    if data.is_fallback:
        return "⚠️ RADAR Incident — AI Unavailable"
    return "🚨 RADAR Incident — RCA"


def _format_actions(actions: Sequence[RecommendedAction]) -> str:
    """A numbered list of actions in ``order``, truncated to fit one section.

    Empty is not expected — the recommendation contract requires at least one
    action — but an empty list renders an honest placeholder rather than a blank
    section, so a contract slip upstream is visible on the card instead of silent.
    """
    if not actions:
        return "_No actions recorded._"
    ordered = sorted(actions, key=lambda a: a.order)
    lines = [f"{i}. {action.action}" for i, action in enumerate(ordered, start=1)]
    return _truncate("\n".join(lines))


def _actions_block(recommendation_id: UUID, *, resolved: bool) -> dict[str, object]:
    """The 👍 / 👎 / Resolve buttons, each tagged for the callback parser.

    ``action_id`` is the button's meaning (from :class:`InteractionAction`, so it cannot
    disagree with the parser); ``value`` is the recommendation id the click acts on —
    the only identity the callback carries, from which the handler derives the incident.
    Resolve is styled ``primary`` (Slack's green) as the affirmative, forward-moving
    action — ``danger`` is reserved for destructive actions, which resolving is not.

    ``resolved`` drops the Resolve button once the incident already is: Slack Block Kit
    buttons cannot be disabled or greyed out, so a resolved incident showing an active
    Resolve button would look clickable while doing nothing new (the second click is
    already handled as a benign no-op — see ``_resolve``'s loser path — but hiding the
    button is the honest UI for it). 👍/👎 stay: rating an RCA's usefulness remains
    meaningful after the incident is closed.
    """
    rec = str(recommendation_id)
    elements = [
        _button("👍 Helpful", InteractionAction.FEEDBACK_UP, rec),
        _button("👎 Not helpful", InteractionAction.FEEDBACK_DOWN, rec),
    ]
    if not resolved:
        elements.append(
            _button("✅ Resolve", InteractionAction.RESOLVE, rec, style="primary")
        )
    return {"type": "actions", "elements": elements}


def _iconize(value: str, icons: dict[str, str]) -> str:
    """``high`` -> ``🟠 High`` — a color cue an on-call engineer reads faster than
    the word alone, especially scanning a channel with several cards in it."""
    icon = icons.get(value, _UNKNOWN_ICON)
    return f"{icon} {value.capitalize()}"


def _button(
    label: str, action: InteractionAction, value: str, *, style: str | None = None
) -> dict[str, object]:
    button: dict[str, object] = {
        "type": "button",
        "text": {"type": "plain_text", "text": label},
        "action_id": action.value,
        "value": value,
    }
    if style is not None:
        button["style"] = style
    return button


def _section(text: str) -> dict[str, object]:
    return {"type": "section", "text": {"type": "mrkdwn", "text": _truncate(text)}}


def _context(text: str) -> dict[str, object]:
    return {"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}


def _field(label: str, value: str) -> dict[str, str]:
    return {"type": "mrkdwn", "text": f"*{label}*\n{value}"}


def _truncate(text: str, limit: int = _SECTION_TEXT_LIMIT) -> str:
    """Keep ``text`` within Slack's per-block limit, marking any cut.

    A card that silently drops content is bad; a card Slack refuses to post
    (block over 3000 chars) is worse — the incident then reaches nobody. So an
    oversized field is cut and marked, and the message still delivers.
    """
    if len(text) <= limit:
        return text
    keep = limit - len(_TRUNCATION_MARKER)
    return text[:keep] + _TRUNCATION_MARKER
