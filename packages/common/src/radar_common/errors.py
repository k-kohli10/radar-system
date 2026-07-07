"""The RADAR error hierarchy.

A single base, :class:`RadarError`, so any RADAR-originated failure can be caught
in one place and counted against the ``radar_errors_total{error_type}`` metric.
Each subclass corresponds to a distinct failure mode the platform handles:

======================  ================================================  =======
Error                   Raised when                                       Maps to
======================  ================================================  =======
ConfigurationError      required config/secret missing or invalid         readyz 503
AuthenticationError     agent token missing or unknown                    401
AuthorizationError      valid caller not permitted (e.g. wrong LLM mode)  403
InvalidPayloadError     request or event payload fails validation         422
NotFoundError           a referenced entity does not exist                404
ConflictError           operation conflicts with existing state           409
UpstreamServiceError    a downstream dependency failed                    503
======================  ================================================  =======

These are domain errors for business logic. The FastAPI auth dependency raises
``HTTPException`` directly at the transport edge; services translate the errors
here into responses where a handler needs to.
"""

from __future__ import annotations


class RadarError(Exception):
    """Base class for every RADAR-originated error."""


class ConfigurationError(RadarError):
    """Required configuration or a secret is missing or invalid at startup."""


class AuthenticationError(RadarError):
    """The agent token was missing or did not match an accepted token."""


class AuthorizationError(RadarError):
    """An authenticated caller is not permitted to perform the action."""


class InvalidPayloadError(RadarError):
    """A request or event payload failed validation."""


class NotFoundError(RadarError):
    """A referenced entity does not exist."""


class ConflictError(RadarError):
    """An operation conflicts with existing state (e.g. a uniqueness clash)."""


class UpstreamServiceError(RadarError):
    """A downstream dependency (LLM provider, database, external API) failed."""
