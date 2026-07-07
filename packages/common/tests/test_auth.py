"""Agent-token authentication tests.

Covers the ``AgentTokenAuth`` unit surface and, per the Phase 3 requirement,
drives the ``X-Radar-Agent-Token`` FastAPI dependency end-to-end through a real
app: a valid token passes, a wrong or missing token is rejected with ``401``.
"""

from __future__ import annotations

import secrets

import pytest
from fastapi import Depends, FastAPI
from fastapi.testclient import TestClient
from pydantic import SecretStr
from radar_common import AGENT_TOKEN_HEADER, AgentTokenAuth

TOKEN = secrets.token_hex(32)
OTHER_TOKEN = secrets.token_hex(32)


def _client(auth: AgentTokenAuth) -> TestClient:
    app = FastAPI()

    @app.post("/events", dependencies=[Depends(auth)])
    async def receive() -> dict[str, str]:
        return {"status": "ok"}

    return TestClient(app)


def test_valid_token_is_accepted() -> None:
    client = _client(AgentTokenAuth([TOKEN]))
    resp = client.post("/events", headers={AGENT_TOKEN_HEADER: TOKEN})
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_wrong_token_is_rejected() -> None:
    client = _client(AgentTokenAuth([TOKEN]))
    resp = client.post("/events", headers={AGENT_TOKEN_HEADER: OTHER_TOKEN})
    assert resp.status_code == 401


def test_missing_token_is_rejected() -> None:
    client = _client(AgentTokenAuth([TOKEN]))
    resp = client.post("/events")
    assert resp.status_code == 401


def test_empty_token_header_is_rejected() -> None:
    client = _client(AgentTokenAuth([TOKEN]))
    resp = client.post("/events", headers={AGENT_TOKEN_HEADER: ""})
    assert resp.status_code == 401


def test_accepts_any_of_several_tokens() -> None:
    client = _client(AgentTokenAuth([TOKEN, OTHER_TOKEN]))
    for token in (TOKEN, OTHER_TOKEN):
        resp = client.post("/events", headers={AGENT_TOKEN_HEADER: token})
        assert resp.status_code == 200


def test_verify_unit() -> None:
    auth = AgentTokenAuth([TOKEN])
    assert auth.verify(TOKEN) is True
    assert auth.verify(OTHER_TOKEN) is False
    assert auth.verify(None) is False
    assert auth.verify("") is False
    assert auth.verify("too-short") is False


def test_accepts_secretstr_tokens() -> None:
    auth = AgentTokenAuth([SecretStr(TOKEN)])
    assert auth.verify(TOKEN) is True


def test_requires_at_least_one_token() -> None:
    with pytest.raises(ValueError):
        AgentTokenAuth([])
    with pytest.raises(ValueError):
        AgentTokenAuth([""])
