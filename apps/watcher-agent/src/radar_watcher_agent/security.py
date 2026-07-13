"""Inbound agent-token authentication for ``POST /events``.

The watcher's only caller is the outbox worker, which presents *the watcher's own*
agent token (each service has its own; the worker holds a map of them). So the
guard here is the ordinary :class:`~radar_common.AgentTokenAuth` over this
service's token — a missing or wrong token is 401.

Two things this module exists to get right:

**1. The token is late-bound.** It is loaded during startup, not at import: a
missing secret must leave ``/readyz`` answering 503, not crash the module before a
probe can ever reach it. So the dependency is built at app-build time over an
accessor, and reads the current value per request — 503 until the token loads, 401
after, for a bad one.

**2. 401 beats 422.** FastAPI decodes and validates the request body *before* route
dependencies run, so a malformed body would raise ``RequestValidationError`` (422)
before the auth dependency ever inspects the token — meaning an unauthenticated
caller could tell a well-formed event from a malformed one, and probe the contract
without credentials. The handler below intercepts every validation error under
``/events`` and answers 401 first when the token is absent or wrong. An
authenticated caller still gets the normal 422. This mirrors the gateway and
ingestion, which enforce the same order for the same reason.

Comparison is constant-time (inherited from ``AgentTokenAuth``); nothing here logs
or embeds a token value.

NOTE for the next agents: the planner and reasoner need this module almost
verbatim. At the third copy it moves to ``radar_common`` — the same rule that
produced the shared ``radar-testing`` fixtures. It is deliberately not extracted at
the first.
"""

# NOTE: no ``from __future__ import annotations`` here. The auth dependency's
# ``Header()`` annotation must stay a real object for FastAPI to resolve it into a
# header parameter (stringized annotations would leave it unresolved), matching
# radar_common.AgentTokenAuth and ingestion's security module.

from collections.abc import Callable, Coroutine
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from radar_common import AGENT_TOKEN_HEADER, AgentTokenAuth

EVENTS_PATH = "/events"
"""The one guarded route. Health and metrics endpoints stay open."""


class EventsAuth:
    """Late-binding ``X-Radar-Agent-Token`` guard for ``POST /events``.

    Constructed with an accessor rather than the token itself, so a secret that has
    not loaded yet answers 503 instead of crashing the app at import.
    """

    def __init__(self, get_auth: Callable[[], AgentTokenAuth | None]) -> None:
        self._get_auth = get_auth

    def current_auth(self) -> AgentTokenAuth | None:
        """The live auth, or ``None`` if the token has not loaded.

        Used by the guarded validation handler to enforce 401-before-422.
        """
        return self._get_auth()

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
                    detail="watcher-agent is not ready",
                )
            if not auth.verify(x_radar_agent_token):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing agent token",
                )

        return dependency


def install_guarded_validation_handler(app: FastAPI, events_auth: EventsAuth) -> None:
    """Make 401 win over 422 on ``/events``.

    Without this, a malformed body is rejected (422) before the token is ever
    checked, which leaks the shape of the contract to an unauthenticated caller: a
    422 says "your body is wrong", which is information they have not earned. With
    it, a bad token is 401 whatever the body looks like, and only an authenticated
    caller learns their payload was malformed.
    """

    @app.exception_handler(RequestValidationError)
    async def _guarded_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if request.url.path == EVENTS_PATH:
            auth = events_auth.current_auth()
            if auth is None:
                # Raising inside an exception handler bubbles as a 500; respond
                # directly.
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"detail": "watcher-agent is not ready"},
                )
            if not auth.verify(request.headers.get(AGENT_TOKEN_HEADER)):
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid or missing agent token"},
                )
        return await request_validation_exception_handler(request, exc)
