# Agent Pipeline

## Pipeline Shape

```
Watcher → Planner → Reasoner
```

Fixed, linear, three stages. Not a graph, not a framework. Just a sequence of
purpose-built services that each do one job and hand off through the outbox.

## Why No Direct HTTP Between Agents

Agents communicate exclusively through a Postgres transactional outbox, dispatched by a
dedicated outbox-worker. An agent never calls another agent's HTTP endpoint directly to
trigger a handoff. See [docs/adr/0003-postgres-outbox.md](../adr/0003-postgres-outbox.md)
for the reasoning. In short: the outbox makes "incident created but no plan requested"
and "plan requested but incident never created" structurally impossible, because the
state change and the event write happen in the same database transaction.

The one exception: reasoner-agent calls `llm-gateway` directly (synchronous request/response,
not a fire-and-forget event), because the reasoner needs the LLM's answer to proceed.

## POST /events Contract

Every agent exposes the same inbound contract for outbox-worker to dispatch into:

```
POST /events
Header: X-Radar-Agent-Token: <token>

Body:
{
  "event_id": "uuid",
  "event_type": "alert.normalized",
  "correlation_id": "uuid",
  "payload": {}
}

200 → processed, or already seen (idempotent)
401 → bad token
422 → malformed payload
```

## Watcher Agent

Correlates raw alerts into incidents.

```
1. Check processed_events → if seen, return 200 (idempotent replay)
2. Load correlation rules from YAML config (ConfigMap in production)
3. Compute fingerprint = sha256(service_name + alert_name + severity)
4. Apply service_groups: alerts from grouped services fold into one incident
5. Query for an open incident with the same fingerprint within the configured window
6. Apply suppression rules. Some alert types suppress follow-on incidents for a
   cooldown period
7. Duplicate found: increment alert_count, apply escalation rules, attach the alert
8. No duplicate: create a new incident, write outbox(incident.plan_requested)
9. All of the above, alert, incident, outbox event, processed_events, audit_log,
   happens in one transaction
```

Correlation rules (window overrides, service groups, suppression, escalation,
fingerprint fields) live in `apps/watcher-agent/config/correlation-rules.yaml`, mounted
as a ConfigMap. Never hardcoded.

## Planner Agent

Builds an investigation plan from a template, no LLM call.

```
1. Check processed_events → if seen, return 200
2. Load plan templates from YAML config (ConfigMap in production)
3. Match template by "service_name:alert_name" key
4. No match → use the _default template
5. Write investigation_plan, outbox(incident.reasoning_requested), processed_events,
   and audit_log, all in one transaction
```

Templates live in `apps/planner-agent/config/plan-templates.yaml`.

## Reasoner Agent

The only stage that calls an LLM.

```
1. Check processed_events → if seen, return 200
2. Load incident and plan from Postgres
3. Build the context bundle (incident metadata + investigation steps, plus
   retrieved runbook context from Phase 8 onward)
4. POST /v1/complete to llm-gateway, mode=extended
5. 503 from gateway → fallback.generate_template_rca(incident, plan) instead. The
   recommendation is built directly from the plan's steps, confidence=low,
   is_fallback=True
6. Parse the RCA into root_cause, confidence, recommended_actions
7. Write recommendation, outbox(recommendation.created), processed_events, and
   audit_log in one transaction. The LLM call itself sits outside the transaction,
   since it's an external network call
```

An incident is never left without a recommendation. See
[docs/adr/0004-llm-gateway.md](../adr/0004-llm-gateway.md) for the fallback chain in
full.

## Idempotency

Every stage checks `processed_events` (keyed on `event_id` + the processing service's
name) before doing any work, and writes its own `processed_events` row in the same
transaction as its other state changes. Combined with outbox-worker's
`SELECT ... FOR UPDATE SKIP LOCKED` polling, an event is processed exactly once per
agent even under concurrent workers or retried dispatches.