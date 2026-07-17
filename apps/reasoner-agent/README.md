# radar-reasoner-agent

The third and final stage of the RADAR incident pipeline, and the only one that
calls an LLM.

Consumes `incident.reasoning_requested` from the planner, builds a context bundle
from the incident and plan ROWS, calls the LLM gateway in `extended` mode, and
stores a root-cause recommendation.

**An incident is never left without a recommendation.** A provider outage, a
timeout, or an unparseable response all fall back to a template RCA built from the
plan's own investigation steps (`is_fallback=true`, `confidence=low`).

See the module docstring in `src/radar_reasoner_agent/__init__.py` for the fallback
contract, and `docs/architecture/agent-pipeline.md` for where it sits.
