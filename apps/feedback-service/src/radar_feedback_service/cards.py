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

The interactive controls (👍 / 👎 / Resolve) are deliberately NOT here yet. A
button Slack renders but nothing handles fails when clicked, so the actions block
lands with its callback handler, not before.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from uuid import UUID

from radar_contracts import RecommendedAction

#: Slack hard-limits a section's text at 3000 chars and rejects the whole message
#: if any block exceeds it. A long root cause is a delivery failure, not a
#: cosmetic overflow, so oversized fields are truncated to fit with a marker. Kept
#: below 3000 to leave room for the surrounding markdown.
_SECTION_TEXT_LIMIT = 2900

_TRUNCATION_MARKER = "… (truncated)"


@dataclass(frozen=True)
class RcaCardData:
    """Exactly what the card needs, read from the two rows by the caller.

    Plain values, not ORM models: the formatter stays decoupled from the database
    and a test builds one in a line. ``severity`` and ``confidence`` are the
    stored strings (the card displays them; it does not reason over them).
    """

    incident_id: UUID
    service_name: str
    title: str
    severity: str
    status: str
    root_cause: str
    confidence: str
    recommended_actions: Sequence[RecommendedAction]
    is_fallback: bool


def format_rca_card(data: RcaCardData) -> tuple[str, list[dict[str, object]]]:
    """Render ``data`` to ``(fallback_text, blocks)`` for a Slack notification.

    ``fallback_text`` is the notification/preview string Slack shows where blocks
    do not render (push notifications, the sidebar) — always meaningful, never
    empty. ``blocks`` is the card layout.
    """
    header = _header_text(data)
    fallback_text = f"{header}: {data.title}"

    blocks: list[dict[str, object]] = [
        {"type": "header", "text": {"type": "plain_text", "text": header}},
        {
            "type": "section",
            "fields": [
                _field("Service", data.service_name),
                _field("Severity", data.severity.upper()),
                _field("Status", data.status),
                _field("Confidence", data.confidence.capitalize()),
            ],
        },
        {"type": "divider"},
        _section(f"*Root cause*\n{data.root_cause}"),
        _section(f"*Recommended actions*\n{_format_actions(data.recommended_actions)}"),
    ]

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
