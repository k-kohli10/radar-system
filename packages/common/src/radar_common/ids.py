"""UUID helpers for RADAR identifiers.

All RADAR identities are UUIDs: database row keys, the logical ``event_id`` an
outbox event carries for idempotency, and the ``correlation_id`` threaded
through the whole incident pipeline. The named generators below make intent
explicit at call sites; :func:`parse_uuid` validates ids arriving as strings in
event payloads, raising :class:`~radar_common.errors.InvalidPayloadError` on a
malformed value so callers surface a clean ``422``.
"""

from __future__ import annotations

from uuid import UUID, uuid4

from .errors import InvalidPayloadError


def new_id() -> UUID:
    """Generate a random UUID for a database row key."""
    return uuid4()


def new_event_id() -> UUID:
    """Generate a new logical event id for an outbox event."""
    return uuid4()


def new_correlation_id() -> UUID:
    """Generate a new correlation id to start a pipeline trace."""
    return uuid4()


def parse_uuid(value: str | UUID, *, field: str = "id") -> UUID:
    """Coerce a string or ``UUID`` into a ``UUID``.

    Accepts an already-parsed ``UUID`` unchanged. Raises
    :class:`~radar_common.errors.InvalidPayloadError` (naming ``field``) when the
    value is not a well-formed UUID — use when reading ids from inbound event
    payloads.
    """
    if isinstance(value, UUID):
        return value
    try:
        return UUID(value)
    except (ValueError, TypeError, AttributeError) as exc:
        raise InvalidPayloadError(f"Invalid UUID for {field!r}: {value!r}") from exc
