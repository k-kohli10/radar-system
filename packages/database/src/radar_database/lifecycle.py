"""The incident lifecycle state machine (ADR 0016).

Four states, and a fixed set of transitions between them::

    open ──▶ investigating ──▶ resolved ──▶ closed
     └──────────────────────────▶ resolved

``open`` may go straight to ``resolved`` (an alert resolves before the reasoner
ever writes a recommendation); otherwise the pipeline moves it to
``investigating`` when the RCA card is delivered. ``closed`` is terminal.

This module is the ONE definition of what a legal transition is. It lives in the
shared database package, not in any single service, because more than one service
performs transitions and they must agree byte-for-byte on the rules:

- ingestion resolves an incident when its last firing alert resolves
  (``{open, investigating} -> resolved``);
- feedback-service moves it to ``investigating`` when it delivers the RCA card
  (``open -> investigating``), resolves it on an engineer's Slack action, and
  closes it on ``@radar close`` (``resolved -> closed``).

Two copies of a state machine that must agree is the same hazard the fingerprint
tuple and CHARS_PER_TOKEN were hoisted to avoid: it looks fine until one copy
changes. So the table, the validity check, and the transition executor all live
here, and every service calls in.

**Actor authority is NOT encoded here.** This module answers "is ``open ->
resolved`` a legal transition?" — a structural question with one answer for the
whole system. It deliberately does not answer "may *ingestion* perform it?"
Coupling the two would mean this shared module had to enumerate every service and
its permissions, and a permission change would edit the state machine. Who may
trigger which transition is ADR 0016's authority table, enforced at the call
site; the split keeps this layer about graph shape alone.
"""

from __future__ import annotations

from uuid import UUID

from radar_common import ConflictError

STATUS_OPEN = "open"
STATUS_INVESTIGATING = "investigating"
STATUS_RESOLVED = "resolved"
STATUS_CLOSED = "closed"

STATES: tuple[str, ...] = (
    STATUS_OPEN,
    STATUS_INVESTIGATING,
    STATUS_RESOLVED,
    STATUS_CLOSED,
)
"""Every incident status, for exhaustive iteration in tests and validation."""

VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_OPEN: frozenset({STATUS_INVESTIGATING, STATUS_RESOLVED}),
    STATUS_INVESTIGATING: frozenset({STATUS_RESOLVED}),
    STATUS_RESOLVED: frozenset({STATUS_CLOSED}),
    STATUS_CLOSED: frozenset(),
}
"""The legal ``current -> next`` edges, straight from ADR 0016.

``closed`` maps to the empty set: it is terminal, and a transition out of it is
rejected like any other illegal edge rather than special-cased. An unknown
current status (a value not in this map) has no legal next state either — see
:func:`is_valid_transition`.
"""

# Per-transition audit event names (ADR 0016). Keyed by the DESTINATION status,
# because the destination is what names the event: entering ``resolved`` is an
# ``incident.resolved`` regardless of whether it came from ``open`` or
# ``investigating``.
_TRANSITION_AUDIT_EVENT: dict[str, str] = {
    STATUS_INVESTIGATING: "incident.investigating",
    STATUS_RESOLVED: "incident.resolved",
    STATUS_CLOSED: "incident.closed",
}

INVALID_TRANSITION_AUDIT_EVENT = "incident.invalid_transition"
"""Audit event a caller records when it catches :class:`InvalidStateTransitionError`.

The rejected attempt is forensic: it is written by the CALLER in its own
transaction, not by :func:`IncidentRepository.transition_status`. The executor
rejects an illegal transition by raising and writing nothing — adding an audit
row and then raising would either vanish on the caller's rollback (a lost record)
or force the caller into a commit-on-exception contract. Recording the attempt is
therefore the service's job; ADR 0016's "must ... write to audit_log" is a
statement about the service, and this constant is the event name it uses.
"""


class InvalidStateTransitionError(ConflictError):
    """An attempt to move an incident along an edge the state machine forbids.

    A :class:`~radar_common.ConflictError` (409): the transition conflicts with the
    incident's current state — most often because the state moved underneath the
    caller (a resolve racing another resolve, the loser seeing ``resolved`` and
    being told ``resolved -> resolved`` is not a thing). Carries both ends of the
    rejected edge so the caller can build the ``incident.invalid_transition`` audit
    payload without re-deriving them.
    """

    def __init__(self, *, from_status: str, attempted_status: str, incident_id: UUID):
        self.from_status = from_status
        self.attempted_status = attempted_status
        self.incident_id = incident_id
        super().__init__(
            f"cannot transition incident {incident_id} "
            f"{from_status!r} -> {attempted_status!r}"
        )


def is_valid_transition(current: str, new: str) -> bool:
    """Whether ``current -> new`` is a legal edge.

    Total over all string inputs: an unknown ``current`` yields no legal next
    state (``.get`` returns the empty set), so a corrupt or unexpected status
    fails closed rather than raising a ``KeyError`` mid-transition.
    """
    return new in VALID_TRANSITIONS.get(current, frozenset())


def transition_audit_event_type(new_status: str) -> str:
    """The ``audit_log.event_type`` for a transition INTO ``new_status``.

    Only ever called for a destination this state machine can reach, so a missing
    key is a programming error, not a runtime condition — hence a ``KeyError``
    rather than a silent fallback that would file a real transition under the
    wrong (or a bland default) event name.
    """
    return _TRANSITION_AUDIT_EVENT[new_status]
