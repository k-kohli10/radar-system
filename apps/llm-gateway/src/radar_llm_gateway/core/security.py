"""Token IAM enforcement: steps 1-5 of the gateway request validation order.

The gateway has no single inbound agent token of its own, so it does not use
``radar_common.AgentTokenAuth``; it validates each caller's token against the
token→mode map loaded from Vault (see ``core.config``) and enforces:

1. Extract ``X-Radar-Agent-Token`` header
2. Token not in the map            -> 401
3. Extract requested mode from the body (done by the route's request model)
4. mode != the token's allowed mode -> 403
5. Estimated input tokens over the mode's ``max_input_tokens`` -> 422

:class:`GatewayAuth` is the FastAPI dependency covering 1-2; it runs before
the body model is validated, so schema errors never mask a 401. One gap
remains: FastAPI decodes the raw JSON *before* dependencies run, so malformed
JSON would 422 even with a bad token. :func:`install_guarded_validation_handler`
closes it — on any validation error under ``/v1/`` it re-checks the token and
answers 401 first, keeping step order exact. Steps 4-5 need the parsed body
and are called by the route handlers.

Input tokens are *estimated* with the provider-neutral ~4-chars-per-token
heuristic: the limits are admission guardrails, not billing, and an estimate
keeps enforcement identical across OpenAI, Anthropic, and Gemini. For embed
requests the budget applies to each input string individually, matching the
per-input semantics of embedding APIs (the default 8191 limit is OpenAI's
per-input cap).

Nothing here logs, and no error detail ever contains a token value or message
content — details carry only service names, mode names, and token counts.
"""

# NOTE: no ``from __future__ import annotations`` here. GatewayAuth is used as
# a FastAPI dependency *instance* (``Depends(auth)``); FastAPI resolves a
# dependency's annotations via ``call.__globals__``, which an instance does not
# have, so stringized annotations would leave ``Header()`` unresolved and the
# token would never be read. Real (non-stringized) annotations avoid that.

from collections.abc import Iterable
from typing import Annotated

from fastapi import FastAPI, Header, HTTPException, Request, Response, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from radar_common import AGENT_TOKEN_HEADER
from radar_contracts import LLMMode, Message

from .config import ModeConfig, TokenGrant, TokenMap

CHARS_PER_TOKEN = 4
"""Heuristic divisor for estimating tokens from character count."""

GUARDED_PATH_PREFIX = "/v1/"
"""Every route under this prefix requires a valid agent token."""


class GatewayAuth:
    """FastAPI dependency resolving the agent token header to a token grant.

    Raises 401 when the header is absent or matches no entry in the token map
    (constant-time comparison inside :meth:`TokenMap.lookup`). Returns the
    matched :class:`TokenGrant` so routes can enforce mode authorization.
    """

    def __init__(self, token_map: TokenMap) -> None:
        self._token_map = token_map

    def grant_for(self, token: str | None) -> TokenGrant | None:
        """Resolve ``token`` to its grant without raising (used by handlers)."""
        return self._token_map.lookup(token)

    async def __call__(
        self,
        x_radar_agent_token: Annotated[str | None, Header()] = None,
    ) -> TokenGrant:
        grant = self.grant_for(x_radar_agent_token)
        if grant is None:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing agent token",
            )
        return grant


def install_guarded_validation_handler(app: FastAPI, auth: GatewayAuth) -> None:
    """Make 401 win over 422 on guarded routes, exactly as the spec orders.

    FastAPI decodes the request JSON before dependencies run, so malformed
    JSON raises ``RequestValidationError`` (422) before :class:`GatewayAuth`
    ever sees the token. This handler intercepts every validation error under
    ``/v1/`` and returns 401 first when the token is missing or unknown;
    authenticated callers get FastAPI's standard 422 response.
    """

    @app.exception_handler(RequestValidationError)
    async def _guarded_validation_error(
        request: Request, exc: RequestValidationError
    ) -> Response:
        if request.url.path.startswith(GUARDED_PATH_PREFIX):
            token = request.headers.get(AGENT_TOKEN_HEADER)
            if auth.grant_for(token) is None:
                # Raising here would bubble as a 500: exceptions raised inside
                # an exception handler are not re-dispatched. Respond directly.
                return JSONResponse(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    content={"detail": "Invalid or missing agent token"},
                )
        return await request_validation_exception_handler(request, exc)


def authorize_mode(grant: TokenGrant, requested: LLMMode) -> None:
    """Step 4: reject with 403 when ``requested`` is not the grant's one mode."""
    if requested != grant.allowed_mode:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=(
                f"Token for service '{grant.service}' is not permitted "
                f"mode '{requested.value}'"
            ),
        )


def estimate_tokens(texts: Iterable[str]) -> int:
    """Estimate the token count of ``texts`` at ~4 characters per token."""
    total_chars = sum(len(text) for text in texts)
    return -(-total_chars // CHARS_PER_TOKEN)  # ceil division


def enforce_chat_budget(
    messages: Iterable[Message], mode: LLMMode, config: ModeConfig
) -> int:
    """Step 5 for ``/v1/complete``: 422 when the whole conversation is over
    the mode's input limit. Returns the estimate for metrics."""
    estimated = estimate_tokens(message.content for message in messages)
    if estimated > config.max_input_tokens:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Input is ~{estimated} tokens, over the '{mode.value}' mode "
                f"limit of {config.max_input_tokens}"
            ),
        )
    return estimated


def enforce_embed_budget(
    inputs: Iterable[str], mode: LLMMode, config: ModeConfig
) -> int:
    """Step 5 for ``/v1/embed``: 422 when any single input is over the mode's
    input limit (embedding limits are per input, not per batch). Returns the
    total estimate for metrics."""
    total = 0
    for position, text in enumerate(inputs, start=1):
        estimated = estimate_tokens((text,))
        if estimated > config.max_input_tokens:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"Input {position} is ~{estimated} tokens, over the "
                    f"'{mode.value}' mode per-input limit of "
                    f"{config.max_input_tokens}"
                ),
            )
        total += estimated
    return total
