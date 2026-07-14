# radar-planner-agent

The second stage of the RADAR incident pipeline.

Consumes `incident.plan_requested` from the watcher, matches a YAML investigation
template on `service_name:alert_name` (falling back to `_default`), stores the
investigation plan, and emits `incident.reasoning_requested`. No LLM.

See the module docstring in `src/radar_planner_agent/__init__.py` for the layout, and
`docs/architecture/agent-pipeline.md` for where it sits in the pipeline.
