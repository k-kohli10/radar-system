"""Service bootstrap wiring tests."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from radar_common import (
    AGENT_TOKEN_HEADER,
    AgentTokenAuth,
    RadarSettings,
    SecretNotFoundError,
    bootstrap,
)
from radar_common.bootstrap import (
    AGENT_TOKEN_PREVIOUS_SECRET,
    AGENT_TOKEN_SECRET,
    load_agent_tokens,
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


def test_load_agent_tokens_current_only(tmp_path: Path) -> None:
    # Steady state: only agent_token is present, so exactly one token is accepted.
    (tmp_path / AGENT_TOKEN_SECRET).write_text("current\n", encoding="utf-8")
    tokens = load_agent_tokens()
    assert [t.get_secret_value() for t in tokens] == ["current"]


def test_load_agent_tokens_accepts_both_during_rotation(tmp_path: Path) -> None:
    """The two-phase guarantee: a target accepts the outgoing token as well as the new.

    This is what closes the transient-401 window a rotation used to open — the worker
    can still be sending the previous token while the target has already flipped to
    the new one, and a 401 is permanent (immediate dead-letter). Written
    mutation-style: drop the previous-token read from ``load_agent_tokens`` and the
    ``"old"`` assertion fails.
    """
    (tmp_path / AGENT_TOKEN_SECRET).write_text("new\n", encoding="utf-8")
    (tmp_path / AGENT_TOKEN_PREVIOUS_SECRET).write_text("old\n", encoding="utf-8")

    auth = AgentTokenAuth(load_agent_tokens())
    assert auth.verify("new") is True
    assert auth.verify("old") is True
    assert auth.verify("stranger") is False


def test_load_agent_tokens_requires_current(tmp_path: Path) -> None:
    # Only the previous token present (an operator slip): current stays required, so
    # startup fails loudly rather than coming up accepting only a retired credential.
    (tmp_path / AGENT_TOKEN_PREVIOUS_SECRET).write_text("old\n", encoding="utf-8")
    with pytest.raises(SecretNotFoundError):
        load_agent_tokens()
