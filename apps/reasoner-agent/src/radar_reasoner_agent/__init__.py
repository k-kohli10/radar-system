"""RADAR reasoner agent: the third and final stage of the incident pipeline.

The only stage that calls an LLM, and the only stage that can spend money. It
consumes ``incident.reasoning_requested``, builds a context bundle from the incident
and investigation plan rows (never from the event itself, which carries only ids),
asks the model for a root-cause analysis, and stores a recommendation.

An incident is never left without a recommendation: any failure (provider down,
timeout, unparseable response) falls back to a template RCA built from the plan's
own steps (``is_fallback=true``, ``confidence=low``) via ``fallback``'s exhaustive
match, which ends in ``assert_never`` so a new failure mode is a type error, not a
silent gap.

Fallback contract: ``llm_provider``/``model_alias``/``model_id`` are always set
(never NULL, so a fallback can't be miscounted as real traffic), while
``raw_llm_response``/``prompt_tokens``/``completion_tokens``/``latency_ms`` are
populated together, read off one ``LLMSuccess | None``, and present iff a call
completed (even one that returned garbage: it still ran, still cost money, and its
output is the only evidence of why the row is a template).

Layout:

- ``config`` - settings, the Postgres DSN, and the OUTBOUND gateway token.
- ``routes`` - ``POST /events``: the ``processed_events`` gate, then the work.
- ``context`` - the bundle the model is shown, built from the rows.
- ``llm`` - the gateway call. Returns a typed result; never raises.
- ``rca`` - parses the model's answer. Returns a typed result; never raises.
- ``fallback`` - the total match, and the one template generator.
- ``main`` - FastAPI assembly. Agent-token guard is shared from ``radar_common``.
"""

from __future__ import annotations

__version__ = "0.7.0"

__all__ = ["__version__"]
