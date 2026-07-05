# Sequence Flows

## 1. Happy Path: Alert to RCA in Slack

```mermaid
sequenceDiagram
    participant Prometheus
    participant ingestion
    participant watcher as watcher-agent
    participant planner as planner-agent
    participant reasoner as reasoner-agent
    participant llm as llm-gateway
    participant feedback as feedback-service
    participant Slack

    Prometheus->>ingestion: POST /alerts/prometheus
    Note over ingestion: normalize, dedupe<br/>INSERT incident+alert+outbox(plan_requested), one tx
    ingestion->>watcher: via outbox-worker
    Note over watcher: correlate, INSERT plan_requested outbox
    watcher->>planner: via outbox-worker
    Note over planner: build plan, INSERT reasoning_requested outbox
    planner->>reasoner: via outbox-worker
    reasoner->>llm: POST /v1/complete
    llm-->>reasoner: RCA completion
    Note over reasoner: INSERT recommendation, outbox(recommendation.created)
    reasoner->>feedback: via outbox-worker
    feedback->>Slack: POST Slack card
```

Every arrow labeled "via outbox-worker" is: agent commits state + outbox row in one
transaction → outbox-worker polls, claims the row (`FOR UPDATE SKIP LOCKED`), and
`POST /events` to the next agent. Agents never call each other directly.

## 2. Deduplication

```
POST /alerts/mock  (OrderProcessingFailureRate, order-service)
  → fingerprint = sha256("order-service:OrderProcessingFailureRate:critical")
  → no open incident with that fingerprint → new incident + outbox event created

POST /alerts/mock  (same alert, 90 seconds later)
  → same fingerprint
  → open incident found within the correlation window (default 5 minutes)
  → alert_count incremented, alert attached to existing incident
  → no new outbox event, no new plan, no new RCA
```

The second POST produces no pipeline work. The engineer sees one incident, not two.

## 3. LLM Fallback

```mermaid
sequenceDiagram
    participant reasoner as reasoner-agent
    participant llm as llm-gateway
    participant feedback as feedback-service
    participant Slack

    reasoner->>llm: POST /v1/complete (mode=extended)
    loop 3 attempts, 1s/3s/9s backoff
        llm->>llm: primary provider call fails
    end
    llm->>llm: fallback provider fails (or none configured)
    llm-->>reasoner: 503
    Note over reasoner: generate_template_rca(incident, plan):<br/>root_cause explains AI was unavailable,<br/>recommended_actions = plan's investigation steps
    Note over reasoner: INSERT recommendation (is_fallback=true, confidence=low)
    reasoner->>feedback: via outbox-worker (recommendation.created)
    feedback->>Slack: POST Slack card ("AI Unavailable" banner + investigation steps)
```

No incident is ever left without a recommendation, even during a full LLM provider
outage. See [docs/adr/0004-llm-gateway.md](../adr/0004-llm-gateway.md).

## 4. Feedback Loop

```mermaid
sequenceDiagram
    actor Engineer
    participant Slack
    participant feedback as feedback-service

    Engineer->>Slack: clicks 👍 Helpful / 👎 Not Helpful / ✏️ Add Correction
    Slack->>feedback: interactive callback
    Note over feedback: validate callback<br/>write feedback row (linked to recommendation + incident)<br/>update incident status if appropriate<br/>increment radar_feedback_total{sentiment}
```

## 5. Slack Bot Query

```mermaid
sequenceDiagram
    actor Engineer
    participant Slack
    participant feedback as feedback-service
    participant Postgres

    Engineer->>Slack: "@radar last 5 incidents for order-service"
    Slack->>feedback: app_mention (Socket Mode locally, Events API + nginx ingress in k8s)
    Note over feedback: bot command parser extracts:<br/>command=last, n=5, service=order-service
    feedback->>Postgres: query existing tables (no new tables, no other service involved)
    Postgres-->>feedback: rows
    feedback->>Slack: reply in the same thread
```

## 6. Outbox Retry and Dead Lettering

```
outbox-worker dispatches event → target agent unreachable / 5xx
  attempt 1: immediate            → fails
  attempt 2: retry at NOW()+5s    → fails
  attempt 3: retry at NOW()+15s   → fails
  attempt 4: retry at NOW()+60s   → fails
  attempt 5: retry at NOW()+300s  → fails
  attempt 6: status → dead_letter, audit_log entry written, metric emitted
```

A dead lettered event stops retrying automatically but is never deleted. It stays
inspectable and requeueable through the outbox worker's admin endpoints.
