# 🔗 Agent Pipeline

## Contents

- 🔲 [Pipeline Shape](#pipeline-shape)
- 🚦 [Why No Direct HTTP Between Agents](#why-no-direct-http-between-agents)
- 📨 [POST /events Contract](#post-events-contract)
- 👁️ [Watcher Agent](#watcher-agent)
- 🗺️ [Planner Agent](#planner-agent)
- 🧠 [Reasoner Agent](#reasoner-agent)
- ♻️ [Idempotency](#idempotency)

## Pipeline Shape

```mermaid
flowchart LR
    Watcher[watcher-agent] --> Planner[planner-agent] --> Reasoner[reasoner-agent]

    classDef agent fill:#eafaf6,stroke:#127d69,color:#0b3d33;
    class Watcher,Planner,Reasoner agent
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

This rule governs pipeline handoffs — the state transitions between watcher-agent,
planner-agent, and reasoner-agent. It is not violated by the reasoner's synchronous calls
to `llm-gateway` and `knowledge-service`: those are supporting services, not stages in the
pipeline (they consume no events and emit none), so a request/response call to one is not
an agent-to-agent handoff and never was in scope for the rule. The reasoner needs the LLM's
answer to proceed, so that call is a direct query, not a fire-and-forget event.

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

```mermaid
flowchart TD
    A[event: alert.normalized] --> B{Seen in processed_events?}
    B -- yes --> R200[Return 200]
    B -- no --> C[Load correlation rules from YAML ConfigMap]
    C --> D["fingerprint = sha256(service_name:alert_name:severity)"]
    D --> E[Fold grouped services into one fingerprint]
    E --> F{Open incident with same fingerprint in window?}
    F -- yes --> G{Suppressed by cooldown rule?}
    G -- yes --> H[Skip outbox write]
    G -- no --> I[Increment alert_count, apply escalation, attach alert]
    F -- no --> J[Create incident, write outbox: incident.plan_requested]
    H --> K[(One transaction: alert + incident + processed_events + audit_log)]
    I --> K
    J --> K

    classDef event fill:#eef3fc,stroke:#2f5fa8,color:#1a2b4a;
    classDef decision fill:#fef6e9,stroke:#b5761f,color:#5a3a0a;
    classDef action fill:#eafaf6,stroke:#127d69,color:#0b3d33;
    classDef terminal fill:#f0f0f0,stroke:#888888,color:#444444;
    classDef commit fill:#eef1fb,stroke:#33418f,color:#1a2350;

    class A event
    class B,F,G decision
    class C,D,E,I,J action
    class R200,H terminal
    class K commit
```

Correlation rules (window overrides, service groups, suppression, escalation,
fingerprint fields) live in `apps/watcher-agent/config/correlation-rules.yaml`, mounted
as a ConfigMap. Never hardcoded.

## Planner Agent

Builds an investigation plan from a template, no LLM call.

```mermaid
flowchart TD
    A[event: incident.plan_requested] --> B{Seen in processed_events?}
    B -- yes --> R200[Return 200]
    B -- no --> C[Load plan templates from YAML ConfigMap]
    C --> D{"Template matches service_name:alert_name?"}
    D -- yes --> E[Use matched template]
    D -- no --> F[Use _default template]
    E --> G[(One transaction: investigation_plan + outbox: reasoning_requested + processed_events + audit_log)]
    F --> G

    classDef event fill:#eef3fc,stroke:#2f5fa8,color:#1a2b4a;
    classDef decision fill:#fef6e9,stroke:#b5761f,color:#5a3a0a;
    classDef action fill:#eafaf6,stroke:#127d69,color:#0b3d33;
    classDef terminal fill:#f0f0f0,stroke:#888888,color:#444444;
    classDef commit fill:#eef1fb,stroke:#33418f,color:#1a2350;

    class A event
    class B,D decision
    class C,E,F action
    class R200 terminal
    class G commit
```

Templates live in `apps/planner-agent/config/plan-templates.yaml`.

## Reasoner Agent

The only stage that calls an LLM.

```mermaid
flowchart TD
    A[event: incident.reasoning_requested] --> B{Seen in processed_events?}
    B -- yes --> R200[Return 200]
    B -- no --> C[Load incident + plan from Postgres]
    C --> D[Build context bundle: incident metadata + investigation steps<br/>+ retrieved runbook context from Phase 8 onward]
    D --> E["POST /v1/complete to llm-gateway, mode=extended"]
    E -- 503 --> F["fallback.generate_template_rca(incident, plan)<br/>confidence=low, is_fallback=true"]
    E -- 200 --> G[Parse root_cause, confidence, recommended_actions]
    F --> H[(One transaction: recommendation + outbox: recommendation.created + processed_events + audit_log)]
    G --> H

    classDef event fill:#eef3fc,stroke:#2f5fa8,color:#1a2b4a;
    classDef decision fill:#fef6e9,stroke:#b5761f,color:#5a3a0a;
    classDef action fill:#eafaf6,stroke:#127d69,color:#0b3d33;
    classDef terminal fill:#f0f0f0,stroke:#888888,color:#444444;
    classDef commit fill:#eef1fb,stroke:#33418f,color:#1a2350;

    class A event
    class B decision
    class C,D,E,F,G action
    class R200 terminal
    class H commit
```

The LLM call itself sits outside the transaction, since it's an external network call.
An incident is never left without a recommendation. See
[docs/adr/0004-llm-gateway.md](../adr/0004-llm-gateway.md) for the fallback chain in
full.

## Idempotency

Every stage checks `processed_events` (keyed on `event_id` + the processing service's
name) before doing any work, and writes its own `processed_events` row in the same
transaction as its other state changes. Combined with outbox-worker's
`SELECT ... FOR UPDATE SKIP LOCKED` polling, an event is processed exactly once per
agent even under concurrent workers or retried dispatches.
