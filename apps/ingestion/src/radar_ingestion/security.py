"""Inbound webhook token authentication for ``/alerts/*``.

Ingestion is the one service that receives requests from outside RADAR's trust
boundary. Per ADR 0011 those external detectors authenticate with a per-source
``X-Radar-Webhook-Token`` — not the internal ``X-Radar-Agent-Token`` — and each
source has its own token (Prometheus's is not Kibana's). A missing or wrong
token is 401; a token valid for one source presented to a different source's
endpoint is also 401 (the check is per source, so cross-source reuse fails).

Each source's token lives in its OWN Vault secret file, so one source's token can
be rotated or revoked without touching the others::

    /vault/secrets/webhook_token_prometheus
    /vault/secrets/webhook_token_kibana
    /vault/secrets/webhook_token_mock

Each file contains just that source's 64-char hex token. Comparison is
constant-time, and nothing here logs or embeds a token value in an error. A
source whose file is absent is not loaded and its endpoint fails closed (every
request to it is 401); at least one token must be present or startup fails.
"""

# NOTE: no ``from __future__ import annotations`` here. The per-source auth
# dependency's ``Header()`` annotation must stay a real object for FastAPI to
# resolve it into a header parameter (stringized annotations are resolved via a
# function's globals, and keeping them real matches radar_common.AgentTokenAuth
# and the gateway's security module).

import hmac
from collections.abc import Callable, Coroutine, Mapping
from pathlib import Path
from typing import Annotated, Any

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from radar_common import ConfigurationError, read_secret

from radar_ingestion.normalizer import AlertSource

GUARDED_PATH_PREFIX = "/alerts/"
"""Every route under this prefix requires a valid per-source webhook token."""

WEBHOOK_TOKEN_HEADER = "X-Radar-Webhook-Token"
"""Header carrying the per-source webhook token on every ``/alerts/*`` request."""

WEBHOOK_TOKEN_SECRET_PREFIX = "webhook_token_"
"""Vault secret filename prefix; one file per source (``…_prometheus``, etc.)."""


def webhook_token_secret_name(source: AlertSource) -> str:
    """Vault secret filename holding ``source``'s webhook token."""
    return f"{WEBHOOK_TOKEN_SECRET_PREFIX}{source.value}"


class WebhookTokenMap:
    """The per-source webhook token table.

    Deliberately not a Pydantic model: the values are secret token strings, so
    the map must never serialize and its ``repr`` shows only source names.
    """

    def __init__(self, tokens: Mapping[AlertSource, str]) -> None:
        if not tokens:
            raise ConfigurationError("webhook token map has no entries")
        self._tokens = dict(tokens)

    def verify(self, source: AlertSource, token: str | None) -> bool:
        """Return whether ``token`` is the configured token for ``source``.

        Constant-time comparison. A source with no configured token, or a
        missing token, returns False (fail closed).
        """
        expected = self._tokens.get(source)
        if expected is None or not token:
            return False
        return hmac.compare_digest(token, expected)

    def __len__(self) -> int:
        return len(self._tokens)

    @property
    def sources(self) -> list[str]:
        """Sorted names of sources that have a configured token (safe to log)."""
        return sorted(s.value for s in self._tokens)

    def __repr__(self) -> str:
        return f"WebhookTokenMap(sources={self.sources})"

    __str__ = __repr__


def load_webhook_tokens(*, directory: Path | None = None) -> WebhookTokenMap:
    """Assemble the token map from the per-source Vault secret files.

    Reads one file per source (``webhook_token_<source>``) so each token can be
    rotated or revoked independently. ``directory`` overrides the secrets
    directory (tests); production reads the init-container mount. A source whose
    file is absent is skipped (its endpoint fails closed); a present-but-empty
    file is a misconfiguration. Raises :class:`~radar_common.ConfigurationError`
    (readyz 503) if no token file is present at all.
    """
    tokens: dict[AlertSource, str] = {}
    for source in AlertSource:
        name = webhook_token_secret_name(source)
        secret = read_secret(name, required=False, directory=directory)
        if secret is None:
            continue
        value = secret.get_secret_value()
        if not value:
            raise ConfigurationError(f"webhook token secret '{name}' is empty")
        tokens[source] = value
    if not tokens:
        expected = ", ".join(webhook_token_secret_name(s) for s in AlertSource)
        raise ConfigurationError(
            f"no webhook token secrets found; expected at least one of: {expected}"
        )
    return WebhookTokenMap(tokens)


class WebhookAuth:
    """Builds per-source FastAPI dependencies over the current token map.

    Constructed with an accessor rather than the map itself: the map is loaded
    during startup (so a missing secret keeps ``/readyz`` at 503 instead of
    crashing import), and each request reads the current value. Until it loads,
    the dependency answers 503; once loaded, a bad or missing token is 401.
    """

    def __init__(self, get_token_map: Callable[[], WebhookTokenMap | None]) -> None:
        self._get_token_map = get_token_map

    def current_token_map(self) -> WebhookTokenMap | None:
        """The token map right now (or None if not yet loaded).

        Used by the guarded validation handler to enforce 401-before-422.
        """
        return self._get_token_map()

    def require(self, source: AlertSource) -> Callable[..., Coroutine[Any, Any, None]]:
        """Return a FastAPI dependency enforcing ``source``'s webhook token."""
        get_token_map = self._get_token_map

        async def dependency(
            x_radar_webhook_token: Annotated[str | None, Header()] = None,
        ) -> None:
            token_map = get_token_map()
            if token_map is None:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail="ingestion is not ready",
                )
            if not token_map.verify(source, x_radar_webhook_token):
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Invalid or missing webhook token",
                )

        return dependency


def install_guarded_webhook_validation_handler(
    app: FastAPI, webhook_auth: WebhookAuth
) -> None:
    """Make 401 win over 422 on ``/alerts/*``, matching the gateway.

    FastAPI decodes the request body before route dependencies run, so a
    malformed body raises ``RequestValidationError`` (422) before the webhook
    auth dependency ever sees the token. This handler intercepts every
    validation error under ``/alerts/`` and, when the token is missing or wrong
    for that path's source, answers 401 first (or 503 if the token map has not
    loaded yet); an authenticated caller still gets the standard 422.
    """
    path_source = {f"{GUARDED_PATH_PREFIX}{s.value}": s for s in AlertSource}

    @app.exception_handler(RequestValidationError)
    async def _guarded_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        source = path_source.get(request.url.path)
        if source is not None:
            token_map = webhook_auth.current_token_map()
            if token_map is None:
                # Raising inside an exception handler bubbles as a 500; respond
                # directly.
                return JSONResponse(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    content={"detail": "ingestion is not ready"},
                )
            token = request.headers.get(WEBHOOK_TOKEN_HEADER)
            if not token_map.verify(source, token):
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid or missing webhook token"},
                )
        return await request_validation_exception_handler(request, exc)
