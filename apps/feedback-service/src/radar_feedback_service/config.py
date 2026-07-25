"""feedback-service configuration and its Vault-mounted secrets.

The same strict split every RADAR service keeps (ADR 0007):

- **Non-secret settings** — service name, log level, the Slack channel and the
  bot's mention name — come from ``RADAR_*`` environment variables via
  :class:`FeedbackSettings`.
- **Secrets** — the Postgres DSN and the Slack bot token — are read from
  Vault-mounted files, never from the environment. A missing file keeps
  ``/readyz`` at 503 rather than crashing an import a probe never sees.

``service_name`` is ``feedback-service``, and that string is load-bearing in three
places that must agree: the ``target_service`` the reasoner writes on its
``recommendation.created`` outbox events, the ``processed_by`` column this service
writes in ``processed_events``, and the Vault path its secrets come from. It is
not cosmetic.

**The channel and the bot name are CONFIG, never constants.** Both are
deployment-specific: the channel a workspace posts to and the name engineers
``@mention`` differ per install, and neither is knowable from this repo. Hardcoding
either is the same class of bug as a service that assumes a port — it works on one
machine and is wrong everywhere else, silently, until an incident goes to a channel
nobody reads.
"""

from __future__ import annotations

from pathlib import Path

from radar_common import RadarSettings, read_secret

POSTGRES_DSN_SECRET = "postgres_dsn"
"""Vault secret filename holding the full Postgres DSN (with password)."""

SLACK_BOT_TOKEN_SECRET = "slack_bot_token"
"""Vault secret filename holding the Slack bot user OAuth token (``xoxb-``)."""

SLACK_APP_TOKEN_SECRET = "slack_app_token"
"""Vault secret filename holding the Slack app-level token (``xapp-``).

Distinct from the bot token: the app-level token authenticates the Socket Mode
WebSocket over which Slack delivers this app's button clicks, while the bot token
(``xoxb-``) backs the Web API calls that post and update cards. Both are required to
go ready — a feedback-service that can post a card but cannot receive the click on it
is half-deaf, which is discovered at the worst possible time. See the interaction
source in the Slack plugin.
"""

SERVICE_NAME = "feedback-service"
"""This service's identity: outbox target, processed_events actor, Vault path."""


class FeedbackSettings(RadarSettings):
    """Non-secret feedback-service settings, read from ``RADAR_*`` env vars."""

    service_name: str = SERVICE_NAME

    #: The Slack channel RCA cards are posted to. Override with
    #: ``RADAR_SLACK_CHANNEL``. Deployment-specific — see the module docstring.
    slack_channel: str = "#all-my-tech"

    #: The name engineers mention to address the bot (``@radar status``). Override
    #: with ``RADAR_SLACK_BOT_NAME``. Used by the bot command parser in a later
    #: commit; declared here so both deployment-specific strings live together.
    slack_bot_name: str = "radar"


def load_postgres_dsn(*, directory: Path | None = None) -> str:
    """Read the Postgres DSN from the ``postgres_dsn`` Vault secret.

    ``directory`` overrides the secrets directory (tests); production reads the
    init-container mount. Raises ``SecretNotFoundError`` when the file is absent,
    failing startup loudly so ``/readyz`` reports 503.
    """
    secret = read_secret(POSTGRES_DSN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret.get_secret_value()


def load_slack_bot_token(*, directory: Path | None = None) -> str:
    """Read the Slack bot token from the ``slack_bot_token`` Vault secret.

    Required at startup even though delivery lands in a later commit: a
    feedback-service without a Slack token cannot do the one thing it exists for,
    and discovering that at the first incident — when an engineer is waiting for a
    card that will never arrive — is strictly worse than refusing to become ready.
    Same reasoning as the planner refusing to start without its templates.
    """
    secret = read_secret(SLACK_BOT_TOKEN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret.get_secret_value()


def load_slack_app_token(*, directory: Path | None = None) -> str:
    """Read the Slack app-level token from the ``slack_app_token`` Vault secret.

    Required at startup: it authenticates the Socket Mode connection that carries
    button clicks back. Without it the RCA cards deliver but every 👍/👎/Resolve click
    goes nowhere — a feedback loop that cannot hear feedback. Failing ready loudly is
    better than a card whose buttons silently do nothing.
    """
    secret = read_secret(SLACK_APP_TOKEN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret.get_secret_value()
