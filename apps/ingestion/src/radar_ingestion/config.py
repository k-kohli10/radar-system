"""Ingestion configuration and the Postgres DSN secret.

Two strictly separated sources, mirroring the platform config/secret split
(docs/adr/0007-vault-init-container.md):

- **Non-secret settings** — service name, log level, environment — come from
  ``RADAR_*`` environment variables via :class:`IngestionSettings`.
- **The Postgres DSN** — which embeds the database password — is a secret, so it
  is read from the Vault-mounted ``postgres_dsn`` file with
  :func:`radar_common.read_secret`, never from the environment. A missing file
  raises ``SecretNotFoundError`` and keeps ``/readyz`` at 503.

The per-source webhook token map is loaded separately in ``security``.
"""

from __future__ import annotations

from pathlib import Path

from radar_common import RadarSettings, read_secret

POSTGRES_DSN_SECRET = "postgres_dsn"
"""Vault secret filename holding the full Postgres DSN (with password)."""


class IngestionSettings(RadarSettings):
    """Non-secret ingestion settings, read from ``RADAR_*`` env vars."""

    service_name: str = "ingestion"


def load_postgres_dsn(*, directory: Path | None = None) -> str:
    """Read the Postgres DSN from the ``postgres_dsn`` Vault secret.

    ``directory`` overrides the secrets directory (tests); production reads the
    init-container mount. Raises ``SecretNotFoundError`` when the file is
    absent, failing startup loudly so ``/readyz`` reports 503.
    """
    secret = read_secret(POSTGRES_DSN_SECRET, directory=directory)
    assert secret is not None  # required=True: read_secret raised if absent
    return secret.get_secret_value()
