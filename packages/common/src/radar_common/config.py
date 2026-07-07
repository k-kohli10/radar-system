"""Configuration and secret loading for RADAR services.

Two strictly separated sources:

- **Non-secret configuration** — service name, log level, endpoint URLs — comes
  from environment variables. Subclass :class:`RadarSettings` per service.
- **Secrets** — agent tokens, API keys, database passwords — NEVER come from the
  environment. A Vault init-container writes each secret to its own file under a
  shared in-memory volume (``/vault/secrets/<name>``) and services read them
  with :func:`read_secret` at startup. See docs/adr/0007-vault-init-container.md.

The secrets directory defaults to ``/vault/secrets`` (the init-container mount)
and is overridable with the ``RADAR_SECRETS_DIR`` environment variable for local
development and tests.
"""

from __future__ import annotations

import os
from pathlib import Path

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigurationError

DEFAULT_SECRETS_DIR = Path("/vault/secrets")
SECRETS_DIR_ENV_VAR = "RADAR_SECRETS_DIR"


def secrets_dir() -> Path:
    """Return the directory Vault secret files are mounted in.

    Defaults to ``/vault/secrets``; override with ``RADAR_SECRETS_DIR``.
    """
    return Path(os.environ.get(SECRETS_DIR_ENV_VAR, str(DEFAULT_SECRETS_DIR)))


class SecretNotFoundError(ConfigurationError):
    """A required secret file was absent from the secrets directory."""

    def __init__(self, name: str, directory: Path) -> None:
        super().__init__(
            f"Required secret {name!r} not found in {directory}. "
            "Is the Vault init-container configured to write it?"
        )
        self.name = name
        self.directory = directory


def read_secret(
    name: str, *, required: bool = True, directory: Path | None = None
) -> SecretStr | None:
    """Read a single Vault secret from ``<secrets_dir>/<name>``.

    The trailing newline that ``vault kv get -field=… > file`` appends is
    stripped. Returns ``None`` when ``required`` is False and the file is
    absent; raises :class:`SecretNotFoundError` when a required secret is
    missing. ``name`` must be a bare filename — path separators are rejected so
    a caller can never escape the secrets directory.
    """
    if not name or "/" in name or "\\" in name or name in (".", ".."):
        raise ValueError(f"Invalid secret name: {name!r}")

    path = (directory or secrets_dir()) / name
    try:
        value = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        if required:
            raise SecretNotFoundError(name, path.parent) from None
        return None
    return SecretStr(value.rstrip("\n"))


class RadarSettings(BaseSettings):
    """Base non-secret settings for every RADAR service.

    Subclass to add service-specific configuration. Reads only from environment
    variables (prefixed ``RADAR_``, case-insensitive); secrets are loaded
    separately via :func:`read_secret` so they always originate from Vault
    files, never the environment.
    """

    model_config = SettingsConfigDict(
        env_prefix="RADAR_",
        extra="ignore",
        case_sensitive=False,
    )

    service_name: str
    environment: str = "production"
    log_level: str = "INFO"
