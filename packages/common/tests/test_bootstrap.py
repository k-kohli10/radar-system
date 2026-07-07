"""Service bootstrap wiring tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from radar_common import (
    AGENT_TOKEN_HEADER,
    RadarSettings,
    SecretNotFoundError,
    bootstrap,
)
from radar_common.config import SECRETS_DIR_ENV_VAR


class _Settings(RadarSettings):
    pass


@pytest.fixture(autouse=True)
def _service_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("RADAR_SERVICE_NAME", "watcher-agent")
    monkeypatch.setenv(SECRETS_DIR_ENV_VAR, str(tmp_path))


def test_bootstrap_builds_settings_and_auth(tmp_path: Path) -> None:
    (tmp_path / "agent_token").write_text("s3cr3t\n", encoding="utf-8")
    rt = bootstrap(_Settings)
    assert rt.settings.service_name == "watcher-agent"
    assert rt.auth is not None
    assert rt.auth.verify("s3cr3t") is True

    # The returned auth actually guards a route end-to-end.
    app = FastAPI()

    @app.post("/events", dependencies=[Depends(rt.auth)])
    async def receive() -> dict[str, str]:
        return {"status": "ok"}

    client = TestClient(app)
    ok = client.post("/events", headers={AGENT_TOKEN_HEADER: "s3cr3t"})
    bad = client.post("/events", headers={AGENT_TOKEN_HEADER: "nope"})
    assert ok.status_code == 200
    assert bad.status_code == 401


def test_bootstrap_without_agent_auth_skips_token(tmp_path: Path) -> None:
    # No agent_token file written; gateway-style startup must still succeed.
    rt = bootstrap(_Settings, with_agent_auth=False)
    assert rt.auth is None
    assert rt.settings.service_name == "watcher-agent"


def test_bootstrap_fails_loudly_when_token_missing() -> None:
    with pytest.raises(SecretNotFoundError):
        bootstrap(_Settings)
