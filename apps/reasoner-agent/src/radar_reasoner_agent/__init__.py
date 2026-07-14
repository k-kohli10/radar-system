"""RADAR reasoner agent: the third and final stage of the incident pipeline.

The only stage that calls an LLM, and the only stage that can spend money. It
consumes ``incident.reasoning_requested``, builds a context bundle from the incident
and the investigation plan, asks the model for a root-cause analysis, and stores a
recommendation.

THE PROPERTY THAT DEFINES THIS SERVICE
--------------------------------------
**An incident is never left without a recommendation.** Not when the provider is
down, not when the call times out, not when the model returns something that is not
the JSON it was asked for. Every one of those falls back to a template RCA built
from the plan's own investigation steps — ``is_fallback=true``, ``confidence=low``
— and the engineer still gets a card telling them what to check.

An RCA that says "AI analysis unavailable, here is your checklist" is useful. An
incident that silently produces nothing is not.

STATE COMES FROM THE ROWS, NEVER FROM THE EVENT
-----------------------------------------------
``incident.reasoning_requested`` carries two ids and nothing else. Severity, status,
and alert_count are read from the ``incidents`` row; the steps from the
``investigation_plans`` row. An incident can escalate between being planned and being
reasoned about, and the card an engineer reads must say what the incident IS — not
what it was when the planner happened to look at it.

THE FALLBACK CONTRACT
---------------------
A fallback row must not be able to lie about itself, and must not corrupt an
aggregate. So:

    is_fallback        true
    llm_provider       "none"                (no provider served this)
    model_alias        "none"                (NOT "extended" — no model ran, and a
                                              GROUP BY must not count fallbacks as
                                              extended-mode traffic)
    model_id           "template-fallback"
    raw_llm_response   NULL                  (there was no response; the template
                                              text is the root_cause)
    prompt_tokens      NULL                  (these describe an LLM call that did
    completion_tokens  NULL                   not happen)
    latency_ms         NULL

The debugging information — which mode we asked for, why we fell back, how long we
waited — lives in ``context_bundle``, where it cannot contaminate a provider or
model aggregate.

Layout:

- ``config`` — settings, the Postgres DSN, and the OUTBOUND gateway token.
- ``routes`` — ``POST /events``: the ``processed_events`` gate, then the work.
- ``main`` — FastAPI assembly. The inbound agent-token guard (and 401-before-422) is
  the shared one from ``radar_common``.
"""

from __future__ import annotations

__version__ = "0.7.0"

__all__ = ["__version__"]
