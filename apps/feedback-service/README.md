# radar-feedback-service

The last stage of the RADAR incident pipeline, and the only one an engineer sees.

Consumes `recommendation.created` from the outbox and delivers the RCA card to
Slack, then handles the interactive feedback (thumbs up/down, resolve) and bot
commands that come back. One deployment, one Slack connection.

See the module docstring in `src/radar_feedback_service/__init__.py` for the
layout, and `docs/architecture/agent-pipeline.md` for where it sits in the
pipeline.
