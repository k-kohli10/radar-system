"""Watcher configuration and the Postgres DSN secret.

The same strict split every RADAR service keeps (docs/adr/0007-vault-init-container.md):

- **Non-secret settings** — service name, log level, environment — come from
  ``RADAR_*`` environment variables via :class:`WatcherSettings`.
- **The Postgres DSN** — which embeds the database password — is a secret, so it is
  read from the Vault-mounted ``postgres_dsn`` file, never from the environment. A
  missing file keeps ``/readyz`` at 503 rather than crashing the import a probe
  never sees.

The watcher's own ``agent_token`` is loaded in ``security``, and the YAML correlation
rules in ``rules``, each next to the thing that uses it.

``service_name`` is ``watcher-agent``, and that string is load-bearing in three places
that must agree: the ``target_service`` ingestion writes on its outbox events, the
``processed_by`` column this service writes in ``processed_events``, and the Vault path
its secrets come from.
"""

from __future__ import annotations

from pathlib import Path

from radar_common import RadarSettings, read_secret

POSTGRES_DSN_SECRET = "postgres_dsn"
"""Vault secret filename holding the full Postgres DSN (with password)."""

SERVICE_NAME = "watcher-agent"
"""This service's identity: outbox target, processed_events actor, Vault path."""


class WatcherSettings(RadarSettings):
    """Non-secret watcher settings, read from ``RADAR_*`` env vars."""

    service_name: str = SERVICE_NAME
    #: The correlation-rules YAML. A ConfigMap mount in production; this repo path
    #: locally. Override with ``RADAR_CORRELATION_RULES_PATH``.
    correlation_rules_path: Path = Path(
        "apps/watcher-agent/config/correlation-rules.yaml"
    )


def load_postgres_dsn(*, directory: Path | None = None) -> str:
    """Read the Postgres DSN from the ``postgres_dsn`` Vault secret.

    ``directory`` overrides the secrets directory (tests); production reads the
    init-container mount. Raises ``SecretNotFoundError`` when the file is absent,
    failing startup loudly so ``/readyz`` reports 503.
    """
    secret = read_secret(POSTGRES_DSN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret.get_secret_value()
