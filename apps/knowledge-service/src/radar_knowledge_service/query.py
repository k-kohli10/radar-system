"""Build the retrieval query string from an incident.

This layer sits ABOVE the retrieval core, and the split is deliberate. The core
takes a plain string, exactly as the ``KnowledgeStore`` protocol declares;
incidents are assembled into a string here. That means the pre-registered probes
in ``tests/retrieval/probes.yaml``, which are plain strings, exercise the SAME
retrieval code path production uses rather than a parallel one built for
measurement.

It is a string join, which sounds too small to test. But a join that silently
drops a component degrades retrieval quality without raising or logging
anything, so the assembly is a named function with its own tests rather than an
f-string at a call site.
"""

from __future__ import annotations

from collections.abc import Sequence

from radar_contracts import PlanStep


def build_query(
    *,
    service_name: str,
    alert_name: str,
    investigation_steps: Sequence[PlanStep] = (),
) -> str:
    """Assemble the text used for both BM25 and embedding.

    Order is ``service_name``, ``alert_name``, then each step's description in
    plan order. It matches how an incident is described from the outside in, and
    it puts the two identifiers first so they survive any future truncation
    against a model's input budget: losing the tail of the investigation steps
    degrades the query, while losing the service and alert changes what is being
    asked.

    ``investigation_steps`` are sorted by ``order`` rather than trusted to arrive
    sorted: they cross a service boundary as JSON, and a query whose meaning
    depends on incidental list order is not reproducible. Steps are optional
    because retrieval must work before the planner has run.

    Raises on blank identifiers. An empty ``service_name`` would produce a query
    missing the term the pre-filter is built around, retrieving plausible chunks
    for the wrong service: a wrong answer rather than an obvious failure.
    """
    if not service_name.strip():
        raise ValueError("service_name is required for a retrieval query")
    if not alert_name.strip():
        raise ValueError("alert_name is required for a retrieval query")

    parts = [service_name.strip(), alert_name.strip()]
    for step in sorted(investigation_steps, key=lambda s: s.order):
        description = step.description.strip()
        if description:
            parts.append(description)
    return " ".join(parts)
