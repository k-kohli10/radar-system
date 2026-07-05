# Sequence Flows

## 1. Happy Path: Alert to RCA in Slack

```
Prometheus                ingestion            watcher-agent   planner-agent  reasoner-agent  llm-gateway  feedback-service   Slack
    │  POST /alerts/prometheus │                    │               │               │              │             │            │
    ├─────────────────────────►│                    │               │               │              │             │            │
    │                          │ normalize, dedupe   │               │               │              │             │            │
    │                          │ INSERT incident+alert+outbox(plan_requested), one tx │              │             │            │
    │                          │────────────────────►│               │               │              │             │            │
    │                          │  (via outbox-worker) │ correlate, INSERT plan_requested outbox       │             │            │
    │                          │                    │───────────────►│               │              │             │            │
    │                          │                    │  (via outbox-worker)          │ build plan, INSERT reasoning_requested outbox │
    │                          │                    │               │──────────────►│              │             │            │
    │                          │                    │               │  (via outbox-worker)          │ POST /v1/complete           │
    │                          │                    │               │               │─────────────►│             │            │
    │                          │                    │               │               │◄─────────────┤             │            │
    │                          │                    │               │               │ INSERT recommendation, outbox(recommendation.created) │
    │                          │                    │               │               │──────────────────────────────────────────►│            │
    │                          │                    │               │               │  (via outbox-worker)                     │ POST Slack card │
    │                          │                    │               │               │                                          │───────────►│
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

```
reasoner-agent  → POST /v1/complete (mode=extended) → llm-gateway
llm-gateway     → primary provider fails, retries exhausted (3 attempts, 1s/3s/9s backoff)
                → fallback provider fails too (or none configured)
                → llm-gateway returns 503
reasoner-agent  → receives 503
                → generate_template_rca(incident, plan): root_cause explains AI was
                  unavailable, recommended_actions = the plan's investigation steps
                → INSERT recommendation (is_fallback=true, confidence=low)
                → outbox(recommendation.created), pipeline continues normally
feedback-service → Slack card renders with an "AI Unavailable" banner, but the
                   engineer still gets the full list of investigation steps
```

No incident is ever left without a recommendation, even during a full LLM provider
outage. See [docs/adr/0004-llm-gateway.md](../adr/0004-llm-gateway.md).

## 4. Feedback Loop

```
Engineer clicks [👍 Helpful] / [👎 Not Helpful] / [✏️ Add Correction] in Slack
  → Slack sends an interactive callback to feedback-service
  → feedback-service validates the callback, writes a feedback row linked to the
    recommendation and incident
  → incident status updates if appropriate
  → radar_feedback_total{sentiment} metric incremented
```

## 5. Slack Bot Query

```
Engineer: "@radar last 5 incidents for order-service"
  → feedback-service receives app_mention via Slack Events API (Socket Mode locally,
    Events API + nginx ingress in Kubernetes)
  → bot command parser extracts: command=last, n=5, service=order-service
  → query existing Postgres tables (no new tables, no other service involved)
  → format and reply in the same thread
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