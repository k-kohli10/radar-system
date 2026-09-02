"""Planner configuration and the Postgres DSN secret.

The same strict split every RADAR service keeps (ADR 0007):

- **Non-secret settings** — service name, log level, the templates path — come
  from ``RADAR_*`` environment variables via :class:`PlannerSettings`.
- **The Postgres DSN** — which embeds the database password — is read from the
  Vault-mounted ``postgres_dsn`` file, never from the environment. A missing file
  keeps ``/readyz`` at 503 rather than crashing an import a probe never sees.

``service_name`` is ``planner-agent``, and that string is load-bearing in three
places that must agree: the ``target_service`` the watcher writes on its outbox
events, the ``processed_by`` column this service writes in ``processed_events``,
and the Vault path its secrets come from.
"""

from __future__ import annotations

from pathlib import Path

from radar_common import RadarSettings, read_secret

POSTGRES_DSN_SECRET = "postgres_dsn"
"""Vault secret filename holding the full Postgres DSN (with password)."""

SERVICE_NAME = "planner-agent"
"""This service's identity: outbox target, processed_events actor, Vault path."""


class PlannerSettings(RadarSettings):
    """Non-secret planner settings, read from ``RADAR_*`` env vars."""

    service_name: str = SERVICE_NAME

    #: The investigation-template YAML. A ConfigMap mount in production; this repo
    #: path locally. Override with ``RADAR_PLAN_TEMPLATES_PATH``.
    plan_templates_path: Path = Path("apps/planner-agent/config/plan-templates.yaml")


def load_postgres_dsn(*, directory: Path | None = None) -> str:
    """Read the Postgres DSN from the ``postgres_dsn`` Vault secret.

    ``directory`` overrides the secrets directory (tests); production reads the
    init-container mount. Raises ``SecretNotFoundError`` when the file is absent,
    failing startup loudly so ``/readyz`` reports 503.
    """
    secret = read_secret(POSTGRES_DSN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret.get_secret_value()
