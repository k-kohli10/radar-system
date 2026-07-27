"""The incident lifecycle transition table (pure — no database).

ADR 0016 defines exactly which ``current -> next`` edges are legal. This pins the
table against that spec edge by edge, over the FULL 4x4 product of states, so a
stray edge added to ``VALID_TRANSITIONS`` (or a legal one dropped) is caught
whether it makes a forbidden transition pass or a permitted one fail. The
real-Postgres behaviour of ``transition_status`` is a separate suite; this one is
about the graph alone.

The expected set is written out here LITERALLY rather than derived from
``VALID_TRANSITIONS`` — a test that computes its oracle from the code under test
proves only that the code equals itself. These are the four edges ADR 0016 lists,
typed by hand.
"""

from __future__ import annotations

from itertools import product

import pytest
from radar_database import (
    STATES,
    STATUS_CLOSED,
    STATUS_INVESTIGATING,
    STATUS_OPEN,
    STATUS_RESOLVED,
    is_valid_transition,
    transition_audit_event_type,
)

# The legal edges, straight from ADR 0016's "Valid Transitions" table — typed by
# hand, not read back from VALID_TRANSITIONS, so this is an independent oracle.
_LEGAL_EDGES: frozenset[tuple[str, str]] = frozenset(
    {
        (STATUS_OPEN, STATUS_INVESTIGATING),
        (STATUS_OPEN, STATUS_RESOLVED),
        (STATUS_INVESTIGATING, STATUS_RESOLVED),
        (STATUS_RESOLVED, STATUS_CLOSED),
    }
)

_ALL_PAIRS: list[tuple[str, str]] = [(a, b) for a, b in product(STATES, STATES)]


def test_all_pairs_is_the_full_product() -> None:
    """Guard the parametrization itself: 4 states => 16 ordered pairs, no fewer.

    An empty or truncated pair list would let the edge test below pass while
    checking nothing — the empty-parametrize trap. This asserts the matrix is
    whole before anything iterates it.
    """
    assert len(_ALL_PAIRS) == 16
    assert len(set(_ALL_PAIRS)) == 16


@pytest.mark.parametrize(("current", "new"), _ALL_PAIRS)
def test_transition_validity_matches_adr_0016(current: str, new: str) -> None:
    """Every one of the 16 edges is legal iff ADR 0016 lists it.

    Both directions in one assertion: a forbidden edge that starts passing and a
    legal edge that starts failing both break this. In particular every edge OUT
    of ``closed`` is checked and must be illegal — ``closed`` is terminal.
    """
    assert is_valid_transition(current, new) is ((current, new) in _LEGAL_EDGES)


def test_closed_is_terminal() -> None:
    """No legal edge leaves ``closed`` — called out explicitly, not just implied."""
    assert all(not is_valid_transition(STATUS_CLOSED, s) for s in STATES)


def test_unknown_current_status_has_no_legal_next() -> None:
    """A corrupt/unexpected status fails closed rather than raising KeyError."""
    assert is_valid_transition("garbage", STATUS_RESOLVED) is False


def test_audit_event_names_match_adr_0016() -> None:
    """The destination status names the audit event, per ADR 0016."""
    assert transition_audit_event_type(STATUS_INVESTIGATING) == "incident.investigating"
    assert transition_audit_event_type(STATUS_RESOLVED) == "incident.resolved"
    assert transition_audit_event_type(STATUS_CLOSED) == "incident.closed"


def test_audit_event_lookup_raises_for_a_non_destination() -> None:
    """``open`` is a start state, never a transition destination — a KeyError, not
    a bland default that would misfile a real transition."""
    with pytest.raises(KeyError):
        transition_audit_event_type(STATUS_OPEN)
