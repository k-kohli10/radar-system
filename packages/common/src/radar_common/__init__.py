"""RADAR shared runtime primitives.

Cross-cutting building blocks every RADAR service reuses, kept vendor-light so
they can be imported anywhere without dragging in a database or web framework
beyond FastAPI:

- ``logging`` — structlog JSON to stdout with ``correlation_id`` bound on every
  log line.
- ``config`` — settings loader that reads secrets from Vault-mounted files
  (never from environment variables).
- ``auth`` — the ``X-Radar-Agent-Token`` FastAPI dependency guarding all
  non-health, non-metrics endpoints.
- ``errors`` — the RADAR exception hierarchy.
- ``ids`` — UUID helpers for event and correlation ids.
- ``time`` — timezone-aware UTC helpers.

Every public symbol is re-exported here, so consumers import from the top
level::

    from radar_common import configure_logging, RadarError, new_correlation_id
"""

from __future__ import annotations

from .auth import (
    AGENT_TOKEN_HEADER,
    EVENTS_PATH,
    AgentTokenAuth,
    EventsAuth,
    install_guarded_events_handler,
)
from .bootstrap import ServiceRuntime, bootstrap
from .config import (
    DEFAULT_SECRETS_DIR,
    RadarSettings,
    SecretNotFoundError,
    read_secret,
    secrets_dir,
)
from .errors import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ConflictError,
    InvalidPayloadError,
    NotFoundError,
    RadarError,
    UpstreamServiceError,
)
from .ids import new_correlation_id, new_event_id, new_id, parse_uuid
from .logging import (
    CORRELATION_ID_KEY,
    bind_log_correlation_id,
    clear_context,
    configure_logging,
    get_logger,
)
from .time import ensure_utc, utcnow
from .timeouts import (
    REASONER_DISPATCH_TIMEOUT_SECONDS,
    REASONER_LLM_BUDGET_SECONDS,
)
from .tokens import CHARS_PER_TOKEN, estimate_tokens

__version__ = "0.3.0"

__all__ = [
    "__version__",
    # logging
    "CORRELATION_ID_KEY",
    "bind_log_correlation_id",
    "clear_context",
    "configure_logging",
    "get_logger",
    # auth
    "AGENT_TOKEN_HEADER",
    "EVENTS_PATH",
    "AgentTokenAuth",
    "EventsAuth",
    "install_guarded_events_handler",
    # bootstrap
    "ServiceRuntime",
    "bootstrap",
    # config
    "DEFAULT_SECRETS_DIR",
    "RadarSettings",
    "SecretNotFoundError",
    "read_secret",
    "secrets_dir",
    # errors
    "RadarError",
    "ConfigurationError",
    "AuthenticationError",
    "AuthorizationError",
    "InvalidPayloadError",
    "NotFoundError",
    "ConflictError",
    "UpstreamServiceError",
    # ids
    "new_id",
    "new_event_id",
    "new_correlation_id",
    "parse_uuid",
    # timeouts
    "REASONER_DISPATCH_TIMEOUT_SECONDS",
    "REASONER_LLM_BUDGET_SECONDS",
    # time
    "utcnow",
    "ensure_utc",
    # tokens
    "CHARS_PER_TOKEN",
    "estimate_tokens",
]
