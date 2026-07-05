# Glossary

**Agent**: One of the three pipeline services (watcher-agent, planner-agent,
reasoner-agent) that processes a single stage of incident handling and hands off via
the outbox. Not an autonomous LLM agent in the LangChain sense. RADAR uses no agent
framework.

**Alert**: A single fired notification from Prometheus alertmanager or Kibana Watcher,
normalized into RADAR's schema on ingest.

**Audit log**: The append-only `audit_log` table. Records significant state
transitions across all entities. Never updated or deleted.

**Confidence**: `low | medium | high`, attached to every recommendation. `low` is
always used for template-fallback RCAs.

**Context bundle**: The structured payload the reasoner builds and sends to the LLM
gateway. It contains incident metadata, investigation steps, and (from Phase 8 onward)
retrieved runbook context.

**Correlation ID**: A UUID assigned when an incident is created (or when an alert is
first ingested), threaded through every alert, plan, recommendation, feedback row,
outbox event, log line, and OTel span belonging to that incident's lifecycle.

**CRAG**: Corrective Retrieval-Augmented Generation. In knowledge-service, each
retrieved runbook chunk is graded (`sufficient | partial | insufficient`) via an LLM
call before being handed to the reasoner. `insufficient` chunks get dropped.

**Dead letter**: An outbox event that exhausted all retry attempts. It stops retrying
automatically, stays in Postgres for inspection, and can be manually requeued.

**Escalation**: A watcher-agent correlation rule that raises an incident's severity
when a configured number of alerts arrive within a time window.

**Fingerprint**: `sha256(service_name + alert_name + severity)`, used by watcher-agent
to detect that a new alert belongs to an already-open incident.

**Incident**: The unit of work RADAR reasons about. One or more correlated alerts
grouped by fingerprint within a correlation window.

**Investigation plan**: The ordered list of investigation steps produced by
planner-agent from a YAML template, matched by `service_name:alert_name`.

**is_fallback**: Boolean flag on a recommendation indicating it was generated from the
investigation plan's steps (template fallback) rather than an actual LLM call, because
all configured LLM providers failed.

**Mode**: One of `fast | reason | extended | embed`. Each LLM gateway agent token is
scoped to exactly one mode, which maps to a specific provider, model, token limit, and
timeout configuration.

**Outbox**: The Postgres `outbox_events` table. The only mechanism by which one agent's
state change triggers work in the next agent, written in the same transaction as the
state change it represents.

**Outbox worker**: The service that polls `outbox_events` (`FOR UPDATE SKIP LOCKED`),
dispatches each event via `POST /events` to its target service, and manages retries and
dead lettering.

**Processed event**: A row in `processed_events` recording that a given
`(event_id, service_name)` pair has already been handled, making event processing
idempotent under retries or redelivery.

**RCA**: Root Cause Analysis. The reasoner's structured output, made up of a root cause
narrative, a confidence level, and recommended actions.

**Recommendation**: The Postgres row storing an RCA, whether LLM-generated or
template-fallback.

**Runbook**: A human-written markdown document describing symptoms, causes,
investigation steps, and resolution for a specific failure mode of a *target* service
(e.g. order-service). RAG-indexed by knowledge-service. Distinct from
`docs/operations/`, which documents operating RADAR itself and is never RAG-indexed.

**Suppression**: A watcher-agent correlation rule that prevents a follow-on incident
from being created for a configured cooldown period after a given alert type fires.

**Vertical slice**: The Phase 7 milestone. The full pipeline (ingestion, watcher,
planner, reasoner, recommendation), proven end to end with real LLM calls. RADAR's
first working proof of concept, tagged `v0.1.0`.
