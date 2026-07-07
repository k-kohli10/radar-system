"""Agent-token authentication for RADAR internal endpoints.

Every internal HTTP call carries an ``X-Radar-Agent-Token`` header holding a
static 64-character hex token (``secrets.token_hex(32)``). Each service loads its
accepted token(s) from Vault at startup and guards every non-health, non-metrics
endpoint with the :class:`AgentTokenAuth` FastAPI dependency, which rejects a
missing or unknown token with ``401``. See docs/adr/0011-inbound-webhook-token.md
and the agent-token security decision in the implementation plan.

Comparison is constant-time and tokens are never logged.

Usage::

    auth = AgentTokenAuth([settings_agent_token])

    @app.post("/events", dependencies=[Depends(auth)])
    async def receive_event(...): ...
"""

# NOTE: no ``from __future__ import annotations`` here. This class is used as a
# FastAPI dependency *instance* (``Depends(auth)``); FastAPI resolves a
# dependency's annotations via ``call.__globals__``, which an instance does not
# have, so stringized annotations would leave ``Header()`` unresolved and the
# token would never be read. Real (non-stringized) annotations avoid that.

import hmac
from collections.abc import Iterable
from typing import Annotated

from fastapi import Header, HTTPException, status
from pydantic import SecretStr

AGENT_TOKEN_HEADER = "X-Radar-Agent-Token"
"""Header name carrying the agent token on every internal request."""


def _unwrap(token: str | SecretStr) -> str:
    return token.get_secret_value() if isinstance(token, SecretStr) else token


class AgentTokenAuth:
    """FastAPI dependency validating the ``X-Radar-Agent-Token`` header.

    Constructed with the token(s) a service accepts (typically its own
    ``agent_token`` from Vault). Use a single-element list for the common case,
    or several when a service accepts more than one caller. Raises ``401`` when
    the header is absent or matches none of the accepted tokens.
    """

    def __init__(self, valid_tokens: Iterable[str | SecretStr]) -> None:
        self._valid = {v for v in (_unwrap(t) for t in valid_tokens) if v}
        if not self._valid:
            raise ValueError("AgentTokenAuth requires at least one non-empty token")

    def verify(self, token: str | None) -> bool:
        """Return whether ``token`` matches an accepted token (constant-time)."""
        if not token:
            return False
        return any(hmac.compare_digest(token, valid) for valid in self._valid)

    async def __call__(
        self,
        x_radar_agent_token: Annotated[str | None, Header()] = None,
    ) -> None:
        if not self.verify(x_radar_agent_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing agent token",
            )
