"""Agent-token authentication for RADAR internal endpoints.

Every internal HTTP call carries an ``X-Radar-Agent-Token`` header holding a
static 64-character hex token (``secrets.token_hex(32)``). Each service loads its
accepted token(s) from Vault at startup and guards every non-health, non-metrics
endpoint with the :class:`AgentTokenAuth` FastAPI dependency, which rejects a
missing or unknown token with ``401``. See docs/adr/0011-inbound-webhook-token.md
and the agent-token security decision in the implementation plan.

Comparison is constant-time and tokens are never logged.

Two layers:

- :class:`AgentTokenAuth` — the raw check. Given the accepted token(s), is this
  request's header one of them?
- :class:`EventsAuth` + :func:`install_guarded_events_handler` — the *agent*
  ``POST /events`` guard, shared by every agent (watcher, planner, reasoner). It adds
  two things on top:

  1. **Late binding.** The token is loaded from Vault during startup, so the
     dependency reads the current value per request: 503 until the token loads, 401
     after, for a bad one. 503 matters to the caller — the outbox worker treats it as
     retryable and backs off, but 401 as PERMANENT, dead-lettering the event over
     what is only a slow start.

  2. **401 beats 422.** A JSON *parse* failure happens during body decoding, BEFORE
     any dependency runs, so without this handler unparseable bytes are answered 422
     and an unauthenticated caller learns the server read their body. The handler
     intercepts validation errors on the guarded path and answers 401 first when the
     token is absent or wrong. An authenticated caller still gets their 422.

Usage in an agent's ``main.py``::

    events_auth = EventsAuth(get_agent_auth, service_name="planner-agent")
    install_guarded_events_handler(app, events_auth)
    app.include_router(create_events_router(events_auth=events_auth, ...))
"""

# NOTE: no ``from __future__ import annotations`` here. These classes are used as
# FastAPI dependency *instances* (``Depends(auth)``), and FastAPI resolves a
# dependency's annotations via ``call.__globals__``, which an instance does not have.
# Stringized annotations would leave ``Header()`` unresolved and the token unread.

import hmac
from collections.abc import Callable, Coroutine, Iterable
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import SecretStr

AGENT_TOKEN_HEADER = "X-Radar-Agent-Token"
"""Header name carrying the agent token on every internal request."""

EVENTS_PATH = "/events"
"""The guarded agent route. Health and metrics endpoints stay open."""


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


class EventsAuth:
    """Late-binding ``X-Radar-Agent-Token`` guard for an agent's ``POST /events``.

    Constructed with an *accessor* rather than the token itself, because the token is
    loaded from Vault during startup: a secret that has not loaded yet answers 503
    (retryable) rather than 401 (permanent, dead-letters the event).
    """

    def __init__(
        self,
        get_auth: Callable[[], AgentTokenAuth | None],
        *,
        service_name: str,
    ) -> None:
        self._get_auth = get_auth
        self._service_name = service_name

    @property
    def not_ready_detail(self) -> str:
        return f"{self._service_name} is not ready"

    def current_auth(self) -> AgentTokenAuth | None:
        """The live auth, or ``None`` if the token has not loaded yet.

        Used by :func:`install_guarded_events_handler` to enforce 401-before-422.
        """
        return self._get_auth()

    def require(self) -> Callable[..., Coroutine[Any, Any, None]]:
        """Return the FastAPI dependency enforcing the agent token."""
        get_auth = self._get_auth
        detail = self.not_ready_detail

        async def dependency(
            x_radar_agent_token: Annotated[str | None, Header()] = None,
        ) -> None:
            auth = get_auth()
            if auth is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail
                )
            if not auth.verify(x_radar_agent_token):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing agent token",
                )

        return dependency


def install_guarded_events_handler(
    app: FastAPI, events_auth: EventsAuth, *, path: str = EVENTS_PATH
) -> None:
    """Make 401 win over 422 on the agent's ``/events`` route.

    Without this, unparseable JSON is rejected (422) before the token is ever checked,
    letting an unauthenticated caller map the contract by probing it. With it, a bad
    token is 401 whatever the body looks like.

    The 422 for a *legitimate* caller is preserved: the outbox worker treats 422 as
    permanent and dead-letters the event, the right answer for a body it can never
    send correctly.
    """

    @app.exception_handler(RequestValidationError)
    async def _guarded_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if request.url.path == path:
            auth = events_auth.current_auth()
            if auth is None:
                # Raising inside an exception handler bubbles as a 500; respond
                # directly.
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"detail": events_auth.not_ready_detail},
                )
            if not auth.verify(request.headers.get(AGENT_TOKEN_HEADER)):
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid or missing agent token"},
                )
        return await request_validation_exception_handler(request, exc)
