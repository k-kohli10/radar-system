"""The ``/v1/embed`` route.

Same validation order as ``/v1/complete``: the :class:`GatewayAuth`
dependency covers steps 1-2 (401), the handler runs step 4
(``authorize_mode`` -> 403) and step 5 (``enforce_embed_budget`` -> 422,
applied per input string) before handing execution to
:class:`GatewayService`; total provider failure maps to 503 via the
app-level handler.

The request/response bodies follow the plan's API contract exactly::

    { "mode": "embed", "input": ["chunk one", "chunk two"] }
    -> { "embeddings": [[...], [...]], "model": "...", "usage": {...} }

``usage.prompt_tokens`` is the gateway's admission estimate — the
``EmbeddingProvider`` contract returns only vectors (see
``gateway/service.py``).

Only ``mode: embed`` is accepted here: other modes' bindings have no
embedding capability, so a mode that got past ``authorize_mode`` (its token
legitimately allows it) would otherwise hit a routing bug instead of a clean
422.
"""

# NOTE: no ``from __future__ import annotations`` — the auth dependency is an
# instance and route annotations must be resolvable at runtime (see
# core/security.py).

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field
from radar_contracts import LLMMode, Usage

from radar_llm_gateway.core.config import TokenGrant
from radar_llm_gateway.core.security import (
    GatewayAuth,
    authorize_mode,
    enforce_embed_budget,
)
from radar_llm_gateway.gateway.service import GatewayService


class EmbedRequest(BaseModel):
    """Body of ``POST /v1/embed``."""

    model_config = ConfigDict(extra="forbid")

    mode: LLMMode = Field(description="Requested gateway mode; must be 'embed'.")
    input: list[str] = Field(min_length=1, description="Texts to embed, in order.")


class EmbedResponse(BaseModel):
    """Body of a successful ``POST /v1/embed`` response."""

    model_config = ConfigDict(extra="forbid")

    embeddings: list[list[float]] = Field(description="One vector per input.")
    model: str = Field(description="Concrete embedding model id.")
    usage: Usage = Field(description="Estimated token accounting for the call.")


def create_embed_router(service: GatewayService, auth: GatewayAuth) -> APIRouter:
    """Build the ``/v1/embed`` route over ``service`` and ``auth``."""
    router = APIRouter()

    @router.post("/v1/embed", response_model_exclude_none=True)
    async def embed(
        request: EmbedRequest,
        grant: Annotated[TokenGrant, Depends(auth)],
    ) -> EmbedResponse:
        authorize_mode(grant, request.mode)
        if request.mode is not LLMMode.EMBED:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=(
                    f"/v1/embed serves only mode 'embed', not '{request.mode.value}'"
                ),
            )
        mode_config = service.mode_config(request.mode)
        estimated = enforce_embed_budget(request.input, request.mode, mode_config)
        result = await service.embed(
            request.mode, request.input, estimated_prompt_tokens=estimated
        )
        return EmbedResponse(
            embeddings=result.embeddings,
            model=result.model,
            usage=Usage(prompt_tokens=result.prompt_tokens),
        )

    return router
