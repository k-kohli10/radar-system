"""The context API: what the reasoner calls to ground an RCA.

``POST /v1/context`` takes an incident-shaped request, assembles the query with
:func:`radar_knowledge_service.query.build_query`, and runs the full retrieval
pipeline: pre-filter, BM25 + kNN, RRF, CRAG grading. The reasoner therefore
exercises exactly the code path the pre-registered probes measured.

EMPTY IS AN ANSWER; UNAVAILABLE IS AN ERROR, AND THE STATUS CODE SEPARATES THEM
-------------------------------------------------------------------------------
``{"chunks": []}`` with 200 means CRAG judged nothing in the corpus relevant.
That claim is the reason the grading stage exists, and it must never be
manufactured from a failure, so retrieval being unavailable (the gateway cannot
embed the query, Elasticsearch is unreachable) answers 503 rather than an empty
200. The reasoner proceeds with no context either way, but the STORED bundle can
say which of the two happened, and collapsing them here would make the audit
trail lie.

Error detail carries the exception CLASS only, never the message: messages from
vendor SDKs can carry request details, the same redaction rule the llm-gateway
applies to provider errors.

WHY THE RESPONSE CARRIES NO SCORE, DESPITE THE PLAN'S v2 SKETCH
----------------------------------------------------------------
The plan's v2 ``retrieved_context`` entry shows a ``score``. That sketch predates
two measured findings: RRF fuses by rank and discards scores, so no single
comparable relevance number survives fusion (BM25 and cosine are on different
scales); and the rerank stage that would have produced one was measured and
removed. The entry carries the ``grade`` instead, which is the judgement the
pipeline actually made.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Any, Protocol

from fastapi import APIRouter, Depends, Header, HTTPException, status
from pydantic import BaseModel, ConfigDict
from radar_common import AgentTokenAuth, get_logger
from radar_contracts import PlanStep

from radar_knowledge_service.query import build_query

log = get_logger("knowledge.api")


class Retriever(Protocol):
    """Retrieval, as the API needs it: the KnowledgeStore.retrieve shape.

    A Protocol rather than the concrete HybridRetriever so the router depends on
    the contract, not the composition; tests satisfy it structurally.
    """

    async def retrieve(
        self, query: str, *, service_name: str | None = ..., limit: int = ...
    ) -> list[dict[str, Any]]: ...


#: The retrieval strategy's answer size: the reasoner consumes at most five
#: graded chunks. Fixed here rather than caller-supplied so one misconfigured
#: caller cannot quietly turn retrieval into a corpus dump.
CONTEXT_LIMIT = 5


class ContextRequest(BaseModel):
    """The incident, as retrieval needs it. Matches ``build_query``'s inputs."""

    model_config = ConfigDict(extra="forbid")

    service_name: str
    alert_name: str
    investigation_steps: list[PlanStep] = []


class ContextChunk(BaseModel):
    """One graded chunk, in the v2 ``retrieved_context`` entry shape.

    ``grade`` is optional because a failed grading call degrades to ungraded
    retrieval rather than raising. An ungraded chunk is one nobody vouched for,
    and the reasoner can tell the difference.
    """

    model_config = ConfigDict(extra="forbid")

    runbook_id: str
    title: str
    section: str
    content: str
    grade: str | None = None
    #: `fixture` until the corpus has had a human review pass. Passed through
    #: so the reasoner knows whether its grounding is reviewed content.
    status: str | None = None


class ContextResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    chunks: list[ContextChunk]


def create_context_router(
    *,
    get_retriever: Callable[[], Retriever | None],
    get_agent_auth: Callable[[], AgentTokenAuth | None],
) -> APIRouter:
    """Build the context router with late-bound dependencies.

    Both dependencies are functions returning ``None`` until startup finishes,
    so before readiness the endpoint answers 503 rather than serving with a
    missing retriever or, worse, no authentication at all.
    """
    router = APIRouter()

    async def require_token(
        x_radar_agent_token: Annotated[str | None, Header()] = None,
    ) -> None:
        auth = get_agent_auth()
        if auth is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="knowledge-service is not ready",
            )
        if not auth.verify(x_radar_agent_token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid or missing agent token",
            )

    @router.post(
        "/v1/context",
        response_model=ContextResponse,
        dependencies=[Depends(require_token)],
    )
    async def context(request: ContextRequest) -> ContextResponse:
        retriever = get_retriever()
        if retriever is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="knowledge-service is not ready",
            )

        try:
            query = build_query(
                service_name=request.service_name,
                alert_name=request.alert_name,
                investigation_steps=request.investigation_steps,
            )
        except ValueError as exc:
            # Blank identifiers. 422 like any other invalid request body.
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc

        try:
            chunks = await retriever.retrieve(
                query,
                service_name=request.service_name,
                limit=CONTEXT_LIMIT,
            )
        except Exception as exc:
            # Retrieval UNAVAILABLE: embed failed, or Elasticsearch unreachable.
            # Never an empty 200, which would manufacture CRAG's "the corpus has
            # nothing for this incident" claim out of an outage. Class name only;
            # vendor messages can carry request detail.
            log.warning(
                "knowledge.context_unavailable",
                service_name=request.service_name,
                error_type=type(exc).__name__,
            )
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=f"retrieval unavailable: {type(exc).__name__}",
            ) from exc

        log.info(
            "knowledge.context_served",
            service_name=request.service_name,
            alert_name=request.alert_name,
            chunks=len(chunks),
            graded=all("grade" in c for c in chunks) if chunks else None,
            empty=not chunks,
        )
        return ContextResponse(chunks=[_to_entry(chunk) for chunk in chunks])

    return router


def _to_entry(chunk: dict[str, Any]) -> ContextChunk:
    """Map a pipeline chunk onto the response shape.

    ``text`` becomes ``content`` (the plan's field name); internal fields (ids,
    per-leg scores, index metadata) stay internal.
    """
    return ContextChunk(
        runbook_id=chunk["runbook_id"],
        title=chunk.get("title", ""),
        section=chunk.get("section", ""),
        content=chunk.get("text", ""),
        grade=chunk.get("grade"),
        status=chunk.get("status"),
    )
