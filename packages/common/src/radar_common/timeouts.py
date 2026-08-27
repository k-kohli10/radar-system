"""Cross-service timeout budgets, and the ordering invariant between them.

These belong to a *relationship* between two services rather than to either one,
so they live here together and are asserted against each other at import.

THE INVARIANT
-------------
When the outbox worker dispatches ``incident.reasoning_requested``, the reasoner
makes remote calls before it can answer. The worker's dispatch timeout must be
strictly LONGER than everything the reasoner can spend. Otherwise the worker gives
up mid-call, classifies the dispatch as retryable, and redelivers the event — and
the reasoner, whose transaction has not committed, finds no ``processed_events``
marker and starts a SECOND LLM call. The platform pays twice, and two
recommendations race for one incident.

The idempotency gate cannot prevent this: it protects against redelivery after a
commit, not during an in-flight call. Only the ordering of these numbers does, and
the failure is silent — it does not crash, it bills twice.

HEAD-OF-LINE COST
-----------------
The worker dispatches a claimed batch sequentially, so a reasoner dispatch can
stall the rest of its batch for up to ``REASONER_DISPATCH_TIMEOUT`` (90s, the wait
for a reasoner that never answers). At POC volume the events behind it are simply
delivered later and nothing is lost, the outbox being durable. Under real load this
wants concurrent dispatch within a batch.
"""

from __future__ import annotations

REASONER_LLM_BUDGET_SECONDS: float = 60.0
"""Hard wall-clock budget for the reasoner's ``/v1/complete`` call.

On overrun the reasoner abandons the LLM and writes a template-fallback RCA, so it
always answers within this. The gateway's own ``extended`` mode allows far longer
(120s per provider attempt, plus internal retries), which is why the reasoner
imposes its own ceiling rather than trusting the gateway's.
"""

REASONER_KNOWLEDGE_BUDGET_SECONDS: float = 20.0
"""Hard wall-clock budget for the reasoner's ``POST /v1/context`` call.

Phase 8 adds a second remote call to the reasoning path: fetching graded runbook
context from the knowledge service BEFORE the LLM call. It joins the same
relationship the LLM budget lives in — the worker's dispatch timeout must outlast
the SUM of everything the reasoner does — which is why it is defined here and in
the invariant below, not as a literal in the reasoner.

Twenty seconds covers the pipeline's normal case (embed ~1s, search ~ms, CRAG
grading a few seconds) with room for a slow grading call. It is deliberately
SHORTER than the knowledge service's own internal CRAG budget (30s): when
grading runs pathologically long, the reasoner hangs up and proceeds without
context rather than spending its margin waiting — retrieval is an enhancement,
and the incident still needs an answer. Same shape as the gateway allowing 120s
while the reasoner caps its own wait at 60.
"""

REASONER_DISPATCH_TIMEOUT_SECONDS: float = 90.0
"""How long the outbox worker waits for the reasoner's ``POST /events``.

Strictly greater than :data:`REASONER_LLM_BUDGET_SECONDS` plus
:data:`REASONER_KNOWLEDGE_BUDGET_SECONDS` — see the invariant above. The margin
absorbs the reasoner's own work around the two remote calls (loading the
incident and plan, parsing, the transactional write).
"""

# The invariant, enforced at import. A future edit that lowers the dispatch timeout
# below the reasoner's spending — or raises either budget past it — fails here, in
# every service that imports this module, rather than silently double-billing
# OpenAI in production. There is no configuration in which the reverse order is
# correct. The SUM matters, not either budget alone: the knowledge call and the
# LLM call happen sequentially on the same dispatch.
assert REASONER_DISPATCH_TIMEOUT_SECONDS > (
    REASONER_LLM_BUDGET_SECONDS + REASONER_KNOWLEDGE_BUDGET_SECONDS
), (
    "the worker must wait LONGER than the reasoner can spend (knowledge fetch + "
    "LLM call), or it will redeliver an event whose calls are still running and "
    "pay for a second one"
)
