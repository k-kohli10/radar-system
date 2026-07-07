"""Config loader and Vault secret-file tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from radar_common import (
    ConfigurationError,
    RadarSettings,
    SecretNotFoundError,
    read_secret,
    secrets_dir,
)
from radar_common.config import SECRETS_DIR_ENV_VAR


def test_settings_read_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_SERVICE_NAME", "watcher-agent")
    monkeypatch.setenv("RADAR_ENVIRONMENT", "local")
    monkeypatch.setenv("RADAR_LOG_LEVEL", "DEBUG")
    settings = RadarSettings()
    assert settings.service_name == "watcher-agent"
    assert settings.environment == "local"
    assert settings.log_level == "DEBUG"


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RADAR_SERVICE_NAME", "planner-agent")
    monkeypatch.delenv("RADAR_ENVIRONMENT", raising=False)
    monkeypatch.delenv("RADAR_LOG_LEVEL", raising=False)
    settings = RadarSettings()
    assert settings.environment == "production"
    assert settings.log_level == "INFO"


def test_read_secret_strips_trailing_newline(tmp_path: Path) -> None:
    # vault kv get -field=... > file appends a trailing newline.
    (tmp_path / "agent_token").write_text("deadbeef\n", encoding="utf-8")
    secret = read_secret("agent_token", directory=tmp_path)
    assert secret is not None
    assert secret.get_secret_value() == "deadbeef"


def test_read_secret_optional_missing_returns_none(tmp_path: Path) -> None:
    assert read_secret("absent", required=False, directory=tmp_path) is None


def test_read_secret_required_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(SecretNotFoundError) as exc:
        read_secret("agent_token", directory=tmp_path)
    # Rooted in the shared error hierarchy.
    assert isinstance(exc.value, ConfigurationError)
    assert exc.value.name == "agent_token"


@pytest.mark.parametrize("name", ["../etc/passwd", "a/b", "a\\b", "", ".", ".."])
def test_read_secret_rejects_path_traversal(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        read_secret(name, directory=tmp_path)


def test_secrets_dir_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(SECRETS_DIR_ENV_VAR, "/custom/secrets")
    assert secrets_dir() == Path("/custom/secrets")


def test_secrets_dir_default(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(SECRETS_DIR_ENV_VAR, raising=False)
    assert secrets_dir() == Path("/vault/secrets")
