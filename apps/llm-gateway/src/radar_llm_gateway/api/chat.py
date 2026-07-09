"""The ``/v1/complete`` route.

Wires the request validation order end to end: the :class:`GatewayAuth`
dependency covers steps 1-2 (401), then the handler runs step 4
(``authorize_mode`` -> 403) and step 5 (``enforce_chat_budget`` -> 422)
before handing execution (steps 7-11) to :class:`GatewayService`. Total
provider failure surfaces as ``AllProvidersFailedError`` and is mapped to 503
by the app-level handler from ``core.errors``.

Streaming responses set their headers before any byte goes out:

- ``Content-Type: text/event-stream``
- ``Cache-Control: no-cache`` — SSE must never be cached
- ``X-Accel-Buffering: no`` — the nginx ingress in front of this service
  buffers responses by default, which would break streaming; this disables
  it per-response.

Because the service primes the stream before the response object is built,
a request whose providers are all dead still gets a clean JSON 503, not a
200 with a broken SSE body.
"""

# NOTE: no ``from __future__ import annotations`` — the auth dependency is an
# instance and route annotations must be resolvable at runtime (see
# core/security.py).

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse, Response, StreamingResponse
from radar_contracts import LLMMode, LLMRequest

from radar_llm_gateway.core.config import TokenGrant
from radar_llm_gateway.core.security import (
    GatewayAuth,
    authorize_mode,
    enforce_chat_budget,
)
from radar_llm_gateway.gateway.service import GatewayService
from radar_llm_gateway.gateway.stream import SSE_MEDIA_TYPE

SSE_HEADERS = {
    "Cache-Control": "no-cache",
    "X-Accel-Buffering": "no",
}
"""Set on every streaming response, before any body bytes."""


def create_chat_router(service: GatewayService, auth: GatewayAuth) -> APIRouter:
    """Build the ``/v1/complete`` route over ``service`` and ``auth``."""
    router = APIRouter()

    @router.post("/v1/complete")
    async def complete(
        request: LLMRequest,
        grant: Annotated[TokenGrant, Depends(auth)],
    ) -> Response:
        authorize_mode(grant, request.mode)
        if request.mode is LLMMode.EMBED:
            # The embed mode's binding has no chat capability; without this
            # guard an embed-token request here would 500 instead of 422.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="mode 'embed' is served by /v1/embed, not /v1/complete",
            )
        mode_config = service.mode_config(request.mode)
        enforce_chat_budget(request.messages, request.mode, mode_config)

        if request.stream:
            frames = await service.stream_sse(request)
            return StreamingResponse(
                frames, media_type=SSE_MEDIA_TYPE, headers=SSE_HEADERS
            )

        response = await service.complete(request)
        return JSONResponse(content=response.model_dump(mode="json"))

    return router
