"""Agent-token authentication for the admin dead-letter endpoints.

The worker's dead-letter admin endpoints touch the database, so they are guarded
by the internal ``X-Radar-Agent-Token`` (the worker's own token) — not open. The
worker loads that token from Vault during startup, so the auth dependency is
built at app-build time over an accessor and late-binds the token: until it
loads, the dependency answers 503; once loaded, a bad or missing token is 401.
Constant-time comparison is reused from :class:`radar_common.AgentTokenAuth`;
nothing here logs or embeds a token value.
"""

# NOTE: no ``from __future__ import annotations`` here. The dependency's
# ``Header()`` annotation must stay a real object for FastAPI to resolve it into a
# header parameter (stringized annotations would leave it unresolved), matching
# radar_common.AgentTokenAuth and ingestion's security module.

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import Header, HTTPException, status
from radar_common import AgentTokenAuth


class AdminAuth:
    """Late-binding ``X-Radar-Agent-Token`` dependency for the admin endpoints.

    Constructed with an accessor rather than the token itself: the worker's
    agent token loads during startup (a missing secret keeps ``/readyz`` at 503
    instead of crashing import), and each request reads the current value.
    """

    def __init__(self, get_auth: Callable[[], AgentTokenAuth | None]) -> None:
        self._get_auth = get_auth

    def require(self) -> Callable[..., Coroutine[Any, Any, None]]:
        """Return the FastAPI dependency enforcing the agent token."""
        get_auth = self._get_auth

        async def dependency(
            x_radar_agent_token: Annotated[str | None, Header()] = None,
        ) -> None:
            auth = get_auth()
            if auth is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="outbox-worker is not ready",
                )
            if not auth.verify(x_radar_agent_token):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing agent token",
                )

        return dependency
