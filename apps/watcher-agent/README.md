# 👁️ radar-watcher-agent

The first stage of the RADAR incident pipeline.

Consumes `alert.normalized` events dispatched by the outbox worker, applies YAML
correlation policy (suppression and escalation) to the incident ingestion already
opened, and requests an investigation plan when one is warranted. No LLM.

See the module docstring in `src/radar_watcher_agent/__init__.py` for the layout,
and `docs/architecture/agent-pipeline.md` for where it sits in the pipeline.
