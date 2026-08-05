# RADAR Implementation Plan
### Version 5.0 - Final. Feed to Claude Code phase by phase.

---

## What RADAR Is

RADAR is an AI-powered Incident Intelligence Platform for SRE workflows.

It ingests pre-fired alerts from Prometheus and Kibana, correlates them into incidents using
configurable rules, retrieves relevant runbooks, reasons over root causes using an LLM, delivers
a structured RCA to the on-call engineer via Slack, collects feedback, and responds to
status queries via a Slack bot.

RADAR does not detect anomalies. Prometheus and Kibana do that. RADAR does correlation,
reasoning, and delivery.

---

## Locked Decisions

Do not revisit these during implementation.

```
Repos              : radar-system only. Single repository. (Superseded entry — see below.)
Namespaces         : radar (app workloads), radar-infra (platform deps)
Agent comms        : Postgres transactional outbox only. No direct HTTP between agents.
Agent pipeline     : Watcher -> Planner -> Reasoner
Agent frameworks   : None. No LangChain, LangGraph, LiteLLM. Ever.
LLM Gateway        : Raw Python. Individual SDKs. anthropic, openai, google-generativeai.
Default provider   : OpenAI (all modes). Others available via config swap.
LLM auth           : Static 32-byte hex token per agent. Vault-stored. One token = one mode.
LLM modes          : fast, reason, extended, embed
LLM fallback       : Gateway tries secondary provider first. If all fail, Reasoner uses
                     template fallback with is_fallback=true. Never skips writing a recommendation.
Detection          : Prometheus alertmanager + Kibana Watcher. Not RADAR.
Watcher ruleset    : YAML config file. Mounted as ConfigMap. Not hardcoded.
Planner templates  : YAML config file. Mounted as ConfigMap. Not hardcoded.
Secrets            : HashiCorp Vault, init-container only. No sidecars. No env vars.
Secret rotation    : Rotate in Vault, restart pod.
Traces             : OTel SDK -> OTel Collector DaemonSet -> Elasticsearch. Kibana APM.
Metrics            : Prometheus scrapes /metrics. Grafana dashboards.
Logs               : structlog JSON -> stdout -> Fluent Bit -> Elasticsearch.
Notifications      : Slack only.
Slack bot          : Lives in feedback-service. Handles both RCA delivery and chat queries.
Ticketing          : None. Incident state in Postgres.
Domain             : E-commerce. Target stub is order-service.
Redis              : Not in this architecture.
Jaeger             : Not in this architecture.
Runbooks           : Human-written markdown about TARGET services. RAG-indexed.
RADAR ops docs     : docs/operations/. Not RAG-indexed.
```

### The one entry that was revisited: Repos

Recorded rather than silently rewritten, because this block says "do not revisit
these" and one of them was revisited — the same discipline as Phase 8's "Added
during implementation" and Phase 9's footprint divergence note. A locked decision
that changes should say so, and say why, instead of quietly reading as though it
had always been that way.

The original entry was `radar-system (product), radar-infra (platform config)`.
**ADR 0018 supersedes ADR 0001 and retires the second repository**: cadence
isolation, the benefit the split existed for, is delivered inside one repo by the
path-based CI that ADR 0001 already committed to building, while the two-repo cost
lands squarely on the Phase 14 fifteen-minute-quickstart done-when. radar-infra was
never created, so nothing had to move.

Note that the **Namespaces** line below it still reads `radar-infra`, and correctly
so. That is a Kubernetes namespace for platform dependencies, not a repository, and
ADR 0018 does not touch it — including the Vault init-container's
`vault.radar-infra.svc.cluster.local` address in the ADR 0007 pattern. Anything
sweeping this document for `radar-infra` must distinguish the two senses.

---

## LLM Provider Strategy

You are buying OpenAI API access. That is your dev provider. All four modes use OpenAI.

Default config:

```yaml
modes:
  fast:
    provider: openai
    model: gpt-4o-mini
    max_input_tokens: 4096
    max_output_tokens: 512
    timeout_seconds: 5
  reason:
    provider: openai
    model: gpt-4o
    max_input_tokens: 8192
    max_output_tokens: 2048
    timeout_seconds: 30
  extended:
    provider: openai
    model: gpt-4o
    max_input_tokens: 32768
    max_output_tokens: 8192
    timeout_seconds: 120
  embed:
    provider: openai
    model: text-embedding-3-small
    max_input_tokens: 8191
    timeout_seconds: 10

fallback:
  extended:
    provider: openai
    model: gpt-4o-mini
  reason:
    provider: openai
    model: gpt-4o-mini
```

Someone else clones the repo and has Anthropic access:

```yaml
modes:
  fast:
    provider: anthropic
    model: claude-haiku-4-5-20251001
  reason:
    provider: anthropic
    model: claude-sonnet-4-6
  extended:
    provider: anthropic
    model: claude-sonnet-4-6
  embed:
    provider: openai        # Anthropic has no embedding model, keep OpenAI for this
    model: text-embedding-3-small
```

One config change. Zero code changes. That is the whole point of the gateway abstraction.

---

## LLM Fallback Chain

When a provider call fails after retries:

```
1. Gateway tries primary provider (e.g. openai/gpt-4o)
2. If primary fails after 3 retries: try fallback provider if configured
3. If fallback also fails: return 503 to caller
4. Reasoner receives 503
5. Reasoner generates template-based RCA from investigation plan steps
6. Writes recommendation with is_fallback=true, confidence=low
7. Slack message is sent with a note that AI analysis was unavailable
8. Engineer still gets actionable investigation steps from the plan
9. Incident is never left without a recommendation
```

This means your on-call engineer always gets something, even during an OpenAI outage.
Low quality is better than nothing at 3am.

---

## E-Commerce Domain

The target stub simulates an `order-service` in an e-commerce platform.

Realistic alert scenarios:

```
OrderProcessingFailureRate    : order failure rate > 5% for 1 minute
CheckoutTimeoutRate           : checkout timeout rate > 3% for 2 minutes
InventoryCheckLatency         : inventory service p95 latency > 2s
PaymentGatewayErrorRate       : payment gateway errors > 2% for 1 minute
OrderServiceHighMemory        : order-service memory usage > 85%
```

Runbooks cover these scenarios with realistic investigation steps.
This looks like a real company's SRE setup. That matters for the portfolio.

---

## Slack Features

The feedback-service owns all Slack interaction. Two distinct features:

### 1. RCA Delivery Cards

When a recommendation is created, feedback-service sends a structured card:

```
[RADAR] Incident: order-service OrderProcessingFailureRate
Severity: critical | Confidence: medium | Service: order-service

Root Cause:
Recent deployment to order-service (v2.4.1) introduced a bug in the
order validation handler causing 8% of orders to fail with a 500 error.

Recommended Actions:
1. Check recent deployments: kubectl rollout history deployment/order-service
2. Review error logs for order-service in the last 30 minutes
3. If deployment is the cause: kubectl rollout undo deployment/order-service
4. Monitor error rate after rollback

[👍 Helpful] [👎 Not Helpful] [✏️ Add Correction]
```

### 2. Slack Bot Chat Queries

Engineers tag the bot to query incident state:

```
@radar status
@radar open incidents
@radar incident INC-abc123
@radar last 5 incidents
@radar last 5 incidents for order-service
@radar summary today
```

Bot connects via Slack Events API (Socket Mode for local dev).
On mention: parse command, query Postgres, format response, reply in thread.

Supported commands v1:

```
@radar status
  -> count of open incidents, last recommendation time, outbox depth

@radar open
  -> list of currently open incidents with service, severity, opened_at

@radar incident <id>
  -> full incident detail: alerts, plan, recommendation, feedback

@radar last <n> [for <service>]
  -> last N incidents, optionally filtered by service name

@radar summary [today|yesterday]
  -> incident count, resolution rate, most affected service
```

Bot lives in feedback-service. No separate deployment. No new database tables needed.
All queries hit existing Postgres tables.

---

## How Detection Works

RADAR receives pre-fired alerts. It does not watch metrics itself.

```
Prometheus evaluates alerting rules every 15 seconds
  -> rule breaches threshold (e.g. order failure rate > 5% for 1 minute)
  -> Prometheus alertmanager fires
  -> POST http://ingestion.radar.svc.cluster.local:8080/alerts/prometheus

Kibana Watcher evaluates log patterns on schedule
  -> pattern matches (e.g. ERROR spike in order-service logs)
  -> POST http://ingestion.radar.svc.cluster.local:8080/alerts/kibana

RADAR ingestion normalizes -> deduplicates -> outbox -> pipeline starts
```

Two distinct sets of alert rules live under `deploy/prometheus/`, told apart by
what they watch, not by where they live. The **e-commerce alert rules** watch the
simulated shop and exist to generate incidents for RADAR to work on
(`alerting-rules.yml`, see "Alert rules" below). **RADAR's own service alerts**
watch RADAR itself — LLM gateway fallback, outbox backlog, agent health — and are
a Phase 10 deliverable. Both are config, not application code.

---

## Watcher Correlation Rules

File: apps/watcher-agent/config/correlation-rules.yaml
Mounted as Kubernetes ConfigMap in production.

```yaml
correlation:
  default_window_minutes: 5

  window_overrides:
    - alert_name: OrderServiceCrashLoop
      window_minutes: 2
    - alert_name: CheckoutTimeoutRate
      window_minutes: 10

  service_groups:
    - name: order-stack
      services: [order-service, order-db, inventory-service]
      group_as_single_incident: true
    - name: checkout-stack
      services: [checkout-service, payment-gateway, order-service]
      group_as_single_incident: true

  suppression:
    - alert_name: OrderServiceHighMemory
      suppress_follow_on_minutes: 10
    - alert_name: InventoryCheckLatency
      suppress_follow_on_minutes: 5

  escalation:
    - alert_count_threshold: 3
      within_minutes: 2
      escalate_to: critical

  fingerprint_fields:
    - service_name
    - alert_name
    - severity
```

---

## Planner Templates

File: apps/planner-agent/config/plan-templates.yaml
Mounted as Kubernetes ConfigMap in production.

```yaml
templates:
  order-service:OrderProcessingFailureRate:
    steps:
      - order: 1
        description: "Check recent deployments: kubectl rollout history deployment/order-service"
      - order: 2
        description: "Review order-service error logs in Kibana for the last 30 minutes"
      - order: 3
        description: "Check order-db connection pool saturation"
      - order: 4
        description: "Check payment-gateway error rate"
      - order: 5
        description: "Review recent config changes to order-service"

  checkout-service:CheckoutTimeoutRate:
    steps:
      - order: 1
        description: "Check checkout-service pod resource usage (CPU/memory)"
      - order: 2
        description: "Review checkout-service timeout logs"
      - order: 3
        description: "Check upstream payment-gateway latency"
      - order: 4
        description: "Check inventory-service response times"
      - order: 5
        description: "Review recent checkout-service deployments"

  order-service:OrderServiceHighMemory:
    steps:
      - order: 1
        description: "Check order-service memory trend over last hour"
      - order: 2
        description: "Review heap dump or memory profile if available"
      - order: 3
        description: "Check for memory leak indicators in recent deployments"
      - order: 4
        description: "Consider restarting pod if memory is critical"

  _default:
    steps:
      - order: 1
        description: "Check recent deployments for the affected service"
      - order: 2
        description: "Review error rate and latency trends in the last 30 minutes"
      - order: 3
        description: "Check upstream and downstream service health"
      - order: 4
        description: "Review recent configuration changes"
```

---

## Repository

### radar-system
The only repository. All app code, packages, plugins, Helm charts, platform
config, docs, tests, CI/CD.

Platform configuration — Helm values for platform deps, Grafana dashboard JSON,
Prometheus alert rules, OTel collector config, Fluent Bit config — is not a
separate repository. It lives under `deploy/`, separated from product code by
directory rather than by repo boundary (ADR 0018).

---

## Kubernetes Namespaces

**radar**
```
ingestion
llm-gateway
outbox-worker
watcher-agent
planner-agent
reasoner-agent
knowledge-service
feedback-service    (includes Slack bot)
```

**radar-infra**
```
postgres
elasticsearch
kibana
prometheus
grafana
vault
```

---

## Home Lab Cluster

```
Control plane : MacBook Air M1, Linux ARM64 VM via UTM, bridged networking
Workers       : Two Lenovo P400 desktops, Ubuntu Server 22.04, x86_64
CNI           : Flannel
Load balancer : MetalLB
Ingress       : nginx ingress controller
Runtime       : containerd
Bootstrap     : kubeadm
```

Every image must build for linux/amd64 AND linux/arm64 via docker buildx.
MacBook control plane is a single point of failure. Accept it and move on.

---

## Final Git Structure

### radar-system

```
radar-system/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml
│   │   ├── cd-ingestion.yml
│   │   ├── cd-llm-gateway.yml
│   │   ├── cd-outbox-worker.yml
│   │   ├── cd-watcher-agent.yml
│   │   ├── cd-planner-agent.yml
│   │   ├── cd-reasoner-agent.yml
│   │   ├── cd-knowledge-service.yml
│   │   └── cd-feedback-service.yml
│   ├── ISSUE_TEMPLATE/
│   │   ├── bug_report.md
│   │   └── feature_request.md
│   └── PULL_REQUEST_TEMPLATE.md
│
├── apps/
│   ├── ingestion/
│   │   ├── src/radar_ingestion/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── routes.py
│   │   │   ├── normalizer.py
│   │   │   ├── deduper.py
│   │   │   └── publisher.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   ├── llm-gateway/
│   │   ├── src/radar_llm_gateway/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── api/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── chat.py
│   │   │   │   ├── embed.py
│   │   │   │   ├── health.py
│   │   │   │   └── metrics.py
│   │   │   ├── core/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── config.py
│   │   │   │   ├── security.py
│   │   │   │   └── errors.py
│   │   │   ├── gateway/
│   │   │   │   ├── __init__.py
│   │   │   │   ├── service.py
│   │   │   │   ├── model_router.py
│   │   │   │   ├── fallback.py
│   │   │   │   ├── retry.py
│   │   │   │   └── stream.py
│   │   │   └── providers/
│   │   │       ├── __init__.py
│   │   │       ├── base.py
│   │   │       ├── anthropic_provider.py
│   │   │       ├── openai_provider.py
│   │   │       └── gemini_provider.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   ├── outbox-worker/
│   │   ├── src/radar_outbox_worker/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── poller.py
│   │   │   ├── dispatcher.py
│   │   │   ├── retry.py
│   │   │   └── dead_letter.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   ├── watcher-agent/
│   │   ├── src/radar_watcher/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── agent.py
│   │   │   ├── correlator.py
│   │   │   └── rules.py
│   │   ├── config/
│   │   │   └── correlation-rules.yaml
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   ├── planner-agent/
│   │   ├── src/radar_planner/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── agent.py
│   │   │   ├── planner.py
│   │   │   └── templates.py
│   │   ├── config/
│   │   │   └── plan-templates.yaml
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   ├── reasoner-agent/
│   │   ├── src/radar_reasoner/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── agent.py
│   │   │   ├── rca.py
│   │   │   ├── context_builder.py
│   │   │   ├── fallback.py
│   │   │   ├── confidence.py
│   │   │   └── prompts.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   ├── knowledge-service/
│   │   ├── src/radar_knowledge/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── api.py
│   │   │   ├── indexer.py
│   │   │   ├── embeddings.py
│   │   │   ├── retrieval.py
│   │   │   ├── ranking.py
│   │   │   └── crag.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   ├── feedback-service/
│   │   ├── src/radar_feedback/
│   │   │   ├── __init__.py
│   │   │   ├── main.py
│   │   │   ├── api.py
│   │   │   ├── slack_delivery.py      # sends RCA cards
│   │   │   ├── slack_events.py        # handles callbacks and bot mentions
│   │   │   ├── slack_bot.py           # parses commands, queries DB, formats responses
│   │   │   ├── classifier.py
│   │   │   └── processor.py
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── pyproject.toml
│   │   └── README.md
│   │
│   └── platform-sim/                  # Local POC only. Never goes to Kubernetes.
│       ├── src/radar_platform_sim/
│       │   ├── __init__.py
│       │   ├── main.py
│       │   ├── metrics.py
│       │   └── chaos.py
│       ├── Dockerfile
│       ├── pyproject.toml
│       └── README.md
│
├── packages/
│   ├── common/
│   │   ├── src/radar_common/
│   │   │   ├── __init__.py
│   │   │   ├── logging.py
│   │   │   ├── config.py
│   │   │   ├── auth.py
│   │   │   ├── errors.py
│   │   │   ├── ids.py
│   │   │   └── time.py
│   │   ├── tests/
│   │   └── pyproject.toml
│   │
│   ├── contracts/
│   │   ├── src/radar_contracts/
│   │   │   ├── __init__.py
│   │   │   ├── alerts.py
│   │   │   ├── incidents.py
│   │   │   ├── events.py
│   │   │   ├── llm.py
│   │   │   ├── feedback.py
│   │   │   ├── notifications.py
│   │   │   ├── bot.py
│   │   │   ├── logs.py
│   │   │   ├── metrics.py
│   │   │   └── traces.py
│   │   ├── tests/
│   │   └── pyproject.toml
│   │
│   ├── database/
│   │   ├── src/radar_database/
│   │   │   ├── __init__.py
│   │   │   ├── connection.py
│   │   │   ├── models.py
│   │   │   ├── outbox.py
│   │   │   ├── repository.py
│   │   │   └── migrations/
│   │   │       └── versions/
│   │   ├── tests/
│   │   └── pyproject.toml
│   │
│   ├── telemetry/
│   │   ├── src/radar_telemetry/
│   │   │   ├── __init__.py
│   │   │   ├── metrics.py
│   │   │   ├── tracing.py
│   │   │   └── events.py
│   │   ├── tests/
│   │   └── pyproject.toml
│   │
│   └── plugin-sdk/
│       ├── src/radar_plugin_sdk/
│       │   ├── __init__.py
│       │   ├── registry.py
│       │   ├── base.py
│       │   ├── loader.py
│       │   └── config.py
│       ├── tests/
│       └── pyproject.toml
│
├── plugins/
│   ├── llm/
│   │   ├── anthropic/
│   │   ├── openai/
│   │   └── gemini/
│   ├── logs/
│   │   └── elastic/
│   ├── metrics/
│   │   └── prometheus/
│   ├── traces/
│   │   └── elastic/
│   └── notifications/
│       └── slack/
│
├── deploy/
│   ├── helm/
│   │   ├── radar/
│   │   │   ├── templates/
│   │   │   │   ├── ingestion/
│   │   │   │   ├── llm-gateway/
│   │   │   │   ├── outbox-worker/
│   │   │   │   ├── watcher-agent/
│   │   │   │   ├── planner-agent/
│   │   │   │   ├── reasoner-agent/
│   │   │   │   ├── knowledge-service/
│   │   │   │   └── feedback-service/
│   │   │   ├── Chart.yaml
│   │   │   └── values.yaml
│   │   └── platform-deps/
│   │       ├── postgres/values.yaml
│   │       ├── elasticsearch/values.yaml
│   │       ├── kibana/values.yaml
│   │       ├── prometheus/values.yaml
│   │       ├── grafana/values.yaml
│   │       └── vault/values.yaml
│   ├── compose/
│   │   └── docker-compose.yml
│   ├── prometheus/
│   │   ├── alerting-rules.yml
│   │   └── prometheus.yml
│   ├── grafana/
│   │   ├── radar-overview.json
│   │   ├── llm-gateway.json
│   │   ├── outbox-health.json
│   │   ├── incident-pipeline.json
│   │   └── feedback-quality.json
│   ├── otel/
│   │   └── config.yml
│   └── fluent-bit/
│       └── config.yml
│
├── docs/
│   ├── adr/
│   │   ├── 0001-monorepo.md
│   │   ├── 0002-fastapi.md
│   │   ├── 0003-postgres-outbox.md
│   │   ├── 0004-llm-gateway.md
│   │   ├── 0005-plugin-architecture.md
│   │   ├── 0006-no-redis.md
│   │   ├── 0007-vault-init-container.md
│   │   ├── 0008-otel-to-elasticsearch.md
│   │   ├── 0009-slack-only-notifications.md
│   │   ├── 0010-external-detection-not-radar.md
│   │   ├── 0011-inbound-webhook-token.md
│   │   └── 0012-cd-approach.md
│   ├── architecture/
│   │   ├── system-overview.md
│   │   ├── agent-pipeline.md
│   │   ├── plugin-architecture.md
│   │   ├── data-model.md
│   │   ├── sequence-flows.md
│   │   └── threat-model.md
│   ├── operations/
│   │   ├── llm-gateway-failure.md
│   │   ├── outbox-backlog.md
│   │   └── vault-secret-rotation.md
│   └── runbooks/
│       ├── order-service-high-failure-rate.md
│       ├── order-service-high-memory.md
│       ├── checkout-timeout-rate.md
│       ├── inventory-check-latency.md
│       └── payment-gateway-errors.md
│
├── tests/
│   ├── integration/
│   ├── e2e/
│   │   └── test_vertical_slice.py
│   └── load/
│
├── scripts/
│   ├── bootstrap.sh
│   ├── detect-changed-services.py
│   ├── seed-dev-data.py
│   └── smoke-test.sh
│
├── .env.example
├── .gitignore
├── .pre-commit-config.yaml
├── CHANGELOG.md
├── CODEOWNERS
├── CONTRIBUTING.md
├── LICENSE
├── Makefile
├── pyproject.toml
├── README.md
└── uv.lock
```

Everything that was previously listed here as a second `radar-infra/` tree is
folded into the `deploy/` block above (ADR 0018): platform-dependency Helm values
at `deploy/helm/platform-deps/`, dashboards at `deploy/grafana/`, alerting rules
and scrape config at `deploy/prometheus/`, collector config at `deploy/otel/`,
and log shipping at `deploy/fluent-bit/`.

That tree was also wrong on a point predating ADR 0018: it listed
`compose/docker-compose.yml` under radar-infra, but the compose stack has lived at
`deploy/compose/docker-compose.yml` in radar-system since Phase 1. Folding the
trees together settles both.

---

## Postgres Schema

### Rules
- UUIDs for all PKs, generated application-side
- All timestamps are TIMESTAMPTZ
- JSONB for flexible payloads only, never for fields you filter on
- Every FK has an index
- Every WHERE column has an index
- Partial indexes on hot paths (outbox poller)
- audit_log is append-only, never update or delete rows

```sql
-- ============================================================
-- alerts
-- ============================================================
CREATE TABLE alerts (
    id                UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    source            VARCHAR(64)  NOT NULL,
    source_alert_id   VARCHAR(256),
    fingerprint       VARCHAR(64)  NOT NULL,
    service_name      VARCHAR(128) NOT NULL,
    alert_name        VARCHAR(256) NOT NULL,
    severity          VARCHAR(32)  NOT NULL,
    status            VARCHAR(32)  NOT NULL DEFAULT 'firing',
    raw_payload       JSONB        NOT NULL,
    labels            JSONB        NOT NULL DEFAULT '{}',
    annotations       JSONB        NOT NULL DEFAULT '{}',
    fired_at          TIMESTAMPTZ  NOT NULL,
    resolved_at       TIMESTAMPTZ,
    received_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    incident_id       UUID         REFERENCES incidents(id),
    correlation_id    UUID         NOT NULL
);

CREATE INDEX idx_alerts_fingerprint    ON alerts(fingerprint);
CREATE INDEX idx_alerts_incident_id    ON alerts(incident_id);
CREATE INDEX idx_alerts_service_name   ON alerts(service_name);
CREATE INDEX idx_alerts_fired_at       ON alerts(fired_at DESC);
CREATE INDEX idx_alerts_status         ON alerts(status);
CREATE INDEX idx_alerts_correlation_id ON alerts(correlation_id);


-- ============================================================
-- incidents
-- ============================================================
CREATE TABLE incidents (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    correlation_id   UUID         NOT NULL UNIQUE,
    fingerprint      VARCHAR(64)  NOT NULL,
    service_name     VARCHAR(128) NOT NULL,
    title            VARCHAR(512) NOT NULL,
    severity         VARCHAR(32)  NOT NULL,
    status           VARCHAR(32)  NOT NULL DEFAULT 'open',
    alert_count      INTEGER      NOT NULL DEFAULT 1,
    opened_at        TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    resolved_at      TIMESTAMPTZ,
    closed_at        TIMESTAMPTZ,
    updated_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_incidents_fingerprint    ON incidents(fingerprint);
CREATE INDEX idx_incidents_status         ON incidents(status);
CREATE INDEX idx_incidents_service_name   ON incidents(service_name);
CREATE INDEX idx_incidents_opened_at      ON incidents(opened_at DESC);
CREATE INDEX idx_incidents_correlation_id ON incidents(correlation_id);


-- ============================================================
-- investigation_plans
-- ============================================================
CREATE TABLE investigation_plans (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id     UUID         NOT NULL REFERENCES incidents(id),
    correlation_id  UUID         NOT NULL,
    steps           JSONB        NOT NULL,
    template_key    VARCHAR(128),
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_plans_incident_id        ON investigation_plans(incident_id);
CREATE UNIQUE INDEX idx_plans_one_per_incident ON investigation_plans(incident_id);


-- ============================================================
-- recommendations
-- ============================================================
CREATE TABLE recommendations (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    incident_id          UUID         NOT NULL REFERENCES incidents(id),
    plan_id              UUID         NOT NULL REFERENCES investigation_plans(id),
    correlation_id       UUID         NOT NULL,
    llm_provider         VARCHAR(64)  NOT NULL,
    model_alias          VARCHAR(64)  NOT NULL,
    model_id             VARCHAR(128) NOT NULL,
    root_cause           TEXT         NOT NULL,
    confidence           VARCHAR(16)  NOT NULL,
    recommended_actions  JSONB        NOT NULL,
    context_bundle       JSONB        NOT NULL DEFAULT '{}',
    raw_llm_response     TEXT,
    is_fallback          BOOLEAN      NOT NULL DEFAULT FALSE,
    prompt_tokens        INTEGER,
    completion_tokens    INTEGER,
    latency_ms           INTEGER,
    created_at           TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_rec_incident_id   ON recommendations(incident_id);
CREATE INDEX idx_rec_plan_id       ON recommendations(plan_id);
CREATE INDEX idx_rec_created_at    ON recommendations(created_at DESC);
CREATE INDEX idx_rec_correlation   ON recommendations(correlation_id);
CREATE INDEX idx_rec_is_fallback   ON recommendations(is_fallback);


-- ============================================================
-- feedback
-- ============================================================
CREATE TABLE feedback (
    id                 UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    recommendation_id  UUID        NOT NULL REFERENCES recommendations(id),
    incident_id        UUID        NOT NULL REFERENCES incidents(id),
    correlation_id     UUID        NOT NULL,
    sentiment          VARCHAR(16) NOT NULL,
    correction_text    TEXT,
    slack_user_id      VARCHAR(64),
    slack_message_ts   VARCHAR(64),
    llm_provider       VARCHAR(64) NOT NULL,
    model_alias        VARCHAR(64) NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_feedback_recommendation_id ON feedback(recommendation_id);
CREATE INDEX idx_feedback_incident_id       ON feedback(incident_id);
CREATE INDEX idx_feedback_sentiment         ON feedback(sentiment);
CREATE INDEX idx_feedback_created_at        ON feedback(created_at DESC);


-- ============================================================
-- outbox_events
-- ============================================================
CREATE TABLE outbox_events (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    event_id        UUID         NOT NULL UNIQUE,
    event_type      VARCHAR(128) NOT NULL,
    target_service  VARCHAR(64)  NOT NULL,
    payload         JSONB        NOT NULL,
    correlation_id  UUID         NOT NULL,
    status          VARCHAR(32)  NOT NULL DEFAULT 'pending',
    attempts        INTEGER      NOT NULL DEFAULT 0,
    last_error      TEXT,
    process_after   TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_outbox_pending ON outbox_events(status, process_after)
    WHERE status IN ('pending', 'processing');
CREATE INDEX idx_outbox_event_id       ON outbox_events(event_id);
CREATE INDEX idx_outbox_correlation_id ON outbox_events(correlation_id);
CREATE INDEX idx_outbox_created_at     ON outbox_events(created_at DESC);


-- ============================================================
-- processed_events
-- Idempotency table. Each agent records event_ids it handled.
-- Composite PK (event_id, processed_by): the same event_id can be marked
-- processed independently by each service, matching the is_already_processed
-- (event_id, processed_by) lookup. The PK index serves that lookup.
-- ============================================================
CREATE TABLE processed_events (
    event_id        UUID        NOT NULL,
    processed_by    VARCHAR(64) NOT NULL,
    processed_at    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (event_id, processed_by)
);


-- ============================================================
-- runbook_documents
-- Index manifest for knowledge-service.
-- ============================================================
CREATE TABLE runbook_documents (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    runbook_id      VARCHAR(128) NOT NULL UNIQUE,
    title           VARCHAR(512) NOT NULL,
    file_path       VARCHAR(512) NOT NULL,
    services        TEXT[]       NOT NULL DEFAULT '{}',
    severity        VARCHAR(32),
    content_hash    VARCHAR(64)  NOT NULL,
    chunk_count     INTEGER      NOT NULL DEFAULT 0,
    indexed_at      TIMESTAMPTZ,
    index_status    VARCHAR(32)  NOT NULL DEFAULT 'pending',
    index_error     TEXT,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_runbook_docs_runbook_id   ON runbook_documents(runbook_id);
CREATE INDEX idx_runbook_docs_services     ON runbook_documents USING GIN(services);
CREATE INDEX idx_runbook_docs_index_status ON runbook_documents(index_status);


-- ============================================================
-- audit_log
-- Append-only. Never update or delete.
-- ============================================================
CREATE TABLE audit_log (
    id              UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    event_type      VARCHAR(128) NOT NULL,
    entity_type     VARCHAR(64)  NOT NULL,
    entity_id       UUID         NOT NULL,
    correlation_id  UUID         NOT NULL,
    actor           VARCHAR(128),
    payload         JSONB        NOT NULL DEFAULT '{}',
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX idx_audit_entity         ON audit_log(entity_type, entity_id);
CREATE INDEX idx_audit_correlation_id ON audit_log(correlation_id);
CREATE INDEX idx_audit_event_type     ON audit_log(event_type);
CREATE INDEX idx_audit_created_at     ON audit_log(created_at DESC);
```

---

## LLM Gateway: Full Specification

### Token IAM

Token format: `secrets.token_hex(32)` (64 char hex string)

Each token maps to exactly one allowed mode in gateway config.
Tokens are loaded from Vault secret files at startup.

### Request Validation Order

```
1. Extract X-Radar-Agent-Token header
2. Token not found in config -> 401
3. Extract requested mode from body
4. mode != token's allowed_mode -> 403
5. Count input tokens against mode max_input_tokens
6. Over limit -> 422
7. Route to provider
8. Hard kill at timeout_seconds
9. On provider failure: retry 3 times, then try fallback provider
10. If fallback also fails: return 503
```

### API Contracts

```
POST /v1/complete
Header: X-Radar-Agent-Token: <token>

Body:
{
  "mode": "extended",
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "..."}
  ],
  "stream": false
}

Response:
{
  "id": "resp_abc123",
  "mode": "extended",
  "provider": "openai",
  "model": "gpt-4o",
  "content": "...",
  "usage": {"prompt_tokens": 1024, "completion_tokens": 512},
  "latency_ms": 3800
}
```

```
POST /v1/embed
Header: X-Radar-Agent-Token: <token>

Body:
{
  "mode": "embed",
  "input": ["chunk one", "chunk two"]
}

Response:
{
  "embeddings": [[0.1, 0.2, ...], [0.3, 0.4, ...]],
  "model": "text-embedding-3-small",
  "usage": {"prompt_tokens": 128}
}
```

### What Never Gets Logged

```
Message content
API keys
Agent tokens
Raw LLM response bodies
```

Logged: mode, provider, model, prompt_tokens, completion_tokens, latency_ms, status_code

### Retry Policy

```
Max retries     : 3
Backoff         : 1s, 3s, 9s
Retry on        : 429, 500, 502, 503, 504, connection timeout
Never retry on  : 400, 401, 403, 422
After retries   : try fallback provider if configured
```

---

## Outbox Worker: Full Specification

### Polling Query

```sql
UPDATE outbox_events
SET status = 'processing', updated_at = NOW()
WHERE id IN (
    SELECT id FROM outbox_events
    WHERE status = 'pending'
      AND process_after <= NOW()
    ORDER BY created_at ASC
    LIMIT 10
    FOR UPDATE SKIP LOCKED
)
RETURNING *;
```

### Retry Delays

```
Attempt 1: immediate
Attempt 2: NOW() + 5s
Attempt 3: NOW() + 15s
Attempt 4: NOW() + 60s
Attempt 5: NOW() + 300s
Attempt 6: status = dead_letter, write to audit_log, emit metric
```

### Idempotency Pattern

Every agent runs this before processing any event:

```python
async def is_already_processed(
    event_id: UUID,
    service_name: str,
    session: AsyncSession
) -> bool:
    result = await session.execute(
        select(ProcessedEvent).where(
            ProcessedEvent.event_id == event_id,
            ProcessedEvent.processed_by == service_name
        )
    )
    return result.scalar_one_or_none() is not None
```

Inserting the ProcessedEvent row happens in the same transaction as all other state changes.

### Graceful Shutdown

SIGTERM -> stop polling -> wait max 30s for in-flight dispatches -> exit.

---

## Agent Pipeline

### Event Flow

```
ingestion        -> outbox_event(alert.normalized,              watcher-agent)
outbox-worker    -> POST /events to watcher-agent
watcher-agent    -> outbox_event(incident.plan_requested,       planner-agent)
outbox-worker    -> POST /events to planner-agent
planner-agent    -> outbox_event(incident.reasoning_requested,  reasoner-agent)
outbox-worker    -> POST /events to reasoner-agent
reasoner-agent   -> POST /v1/complete to llm-gateway  (direct, not via outbox)
reasoner-agent   -> outbox_event(recommendation.created,        feedback-service)
outbox-worker    -> POST /events to feedback-service
feedback-service -> POST to Slack API
```

### POST /events contract (all agents)

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

200 -> processed or already seen (idempotent)
401 -> bad token
422 -> malformed payload
```

### Watcher Agent Logic

```
1. Check processed_events -> if seen, return 200
2. Load correlation rules from YAML config
3. Compute fingerprint = sha256(service_name + alert_name + severity)
4. Apply service_groups: if service is in a group, use group fingerprint
5. Query for open incident with same fingerprint within window
6. Apply suppression rules: if suppressed, skip outbox write
7. If duplicate found:
   - Increment alert_count
   - Apply escalation rules
   - INSERT alert linked to incident
   - INSERT processed_events
   - INSERT audit_log
   - All one transaction
8. If new:
   - INSERT incident
   - INSERT alert
   - INSERT outbox_event(plan_requested)
   - INSERT processed_events
   - INSERT audit_log
   - All one transaction
```

### Planner Agent Logic

```
1. Check processed_events -> if seen, return 200
2. Load plan templates from YAML config
3. Match template by "service_name:alert_name" key
4. If no match: use _default template
5. INSERT investigation_plan
6. INSERT outbox_event(reasoning_requested)
7. INSERT processed_events
8. INSERT audit_log
9. All one transaction
```

### Reasoner Agent Logic

```
1. Check processed_events -> if seen, return 200
2. Load incident and plan from Postgres
3. Build context bundle
4. POST /v1/complete to llm-gateway (mode=extended)
5. If 503 returned: call fallback.generate_template_rca(incident, plan)
6. Parse RCA into root_cause, confidence, recommended_actions
7. INSERT recommendation (is_fallback=True if LLM unavailable)
8. INSERT outbox_event(recommendation.created)
9. INSERT processed_events
10. INSERT audit_log
11. All one transaction (except LLM call)
```

v1 context bundle:
```json
{
  "incident_id": "uuid",
  "service_name": "order-service",
  "alert_name": "OrderProcessingFailureRate",
  "severity": "critical",
  "opened_at": "2025-01-15T10:30:00Z",
  "alert_count": 3,
  "investigation_steps": [
    {"order": 1, "description": "Check recent deployments"},
    {"order": 2, "description": "Review error logs"}
  ],
  "retrieved_context": []
}
```

> **Note (Phase 7 as-built):** this is the bundle SENT to the model — the flat
> prompt-facing shape. What is STORED in `recommendations.context_bundle` is a wrapper
> that composes it with fallback metadata: `{"bundle": {…the v1 bundle above…},
> "fallback": null | {"reason", "attempted_mode", "detail", "elapsed_ms"}}`. The nesting
> is deliberate — it keeps "what the model saw" byte-for-byte reconstructable and
> separate from "why we fell back", and keeps fallback fields out of the prompt (the
> bundle is `extra="forbid"` and serialized straight into the request). See
> `reasoner-agent/fallback.py`.

v2 context bundle (after Phase 8):
```json
{
  ...same as v1...,
  "retrieved_context": [
    {
      "runbook_id": "runbook-001",
      "title": "Order Service High Failure Rate",
      "content": "...",
      "score": 0.91,
      "grade": "sufficient"
    }
  ]
}
```

Reasoner system prompt (v1):
```
You are an SRE incident analysis assistant.
You will be given incident metadata and a structured investigation plan.
Respond ONLY with a valid JSON object. No text before or after it.

Schema:
{
  "root_cause": "your best assessment of the likely root cause",
  "confidence": "low|medium|high",
  "recommended_actions": [
    {"order": 1, "action": "specific actionable step"},
    {"order": 2, "action": "specific actionable step"}
  ]
}

Rules:
- Do not hallucinate metrics, log lines, or deployment names you were not given.
- If you cannot determine a root cause, set confidence=low and explain in root_cause.
- Actions must be specific, not generic. Bad: "check logs". Good: "check order-service
  error logs in Kibana for the last 30 minutes filtered by status=500".
```

Template fallback RCA (when LLM unavailable):
```python
def generate_template_rca(incident: Incident, plan: InvestigationPlan) -> dict:
    return {
        "root_cause": (
            f"AI analysis unavailable due to LLM provider outage. "
            f"Manual investigation required for {incident.service_name} "
            f"{incident.alert_name}."
        ),
        "confidence": "low",
        "recommended_actions": [
            {"order": step["order"], "action": step["description"]}
            for step in plan.steps
        ]
    }
```

---

## Feedback Service and Slack Bot

### RCA Delivery Card

```
*[RADAR] Incident Alert*
Service: order-service | Severity: critical | Confidence: medium

*Root Cause*
Recent deployment to order-service introduced a bug in the order validation
handler causing 8% of orders to fail.

*Recommended Actions*
1. Check recent deployments: kubectl rollout history deployment/order-service
2. Review error logs in Kibana for order-service (last 30 minutes)
3. If deployment is cause: kubectl rollout undo deployment/order-service
4. Monitor error rate after rollback

Incident ID: INC-abc123 | Opened: 10:30 UTC

[👍 Helpful] [👎 Not Helpful] [✏️ Correction]
```

If is_fallback=true, card shows:
```
*[RADAR] Incident Alert - AI Unavailable*
AI analysis could not be completed. Investigation steps provided from runbook.
```

### Slack Bot Commands

```
@radar status
  Response: "3 open incidents. Last RCA: 4 minutes ago. Outbox depth: 0."

@radar open
  Response: Table of open incidents with ID, service, severity, opened_at.

@radar incident INC-abc123
  Response: Full incident details. Alerts, plan steps, RCA, feedback received.

@radar last 5
  Response: Last 5 incidents regardless of service.

@radar last 5 for order-service
  Response: Last 5 incidents for order-service only.

@radar summary today
  Response: Total incidents, resolved count, most affected service, avg resolution time.
```

Bot implementation:
```
Connects via Slack Events API
In local dev: Socket Mode (no public URL needed)
In Kubernetes: Events API with nginx ingress exposing the webhook
On app_mention event: parse text, route to handler, query Postgres, format reply
All bot responses are ephemeral replies in the same thread as the mention
```

---

## Platform Simulator Service

Local POC only. Never deployed to Kubernetes. Lives at `apps/platform-sim/`
(package `radar_platform_sim`).

A SINGLE-PROCESS simulator of a multi-service e-commerce platform — not a set of
microservices, and never will be. One process exposes a domain metric and a chaos
endpoint per scenario; the alert rule watching each metric carries the `service`
label of the service being simulated. That is how one process fires alerts
labelled `service=payment-gateway` and `service=order-service` without either
service existing. The service label lives in the rule, not in the metric.

Originally scoped as an order-service stub (`apps/order-stub`). Extended before
Phase 8 so that every Tier-1 runbook has a matching fireable alert: a runbook
describing an alert nothing can fire is dead corpus that no e2e will ever
retrieve.

```
GET  /metrics
     Exposes, chaos-driven:
     order_processing_failure_rate  (gauge, 0.0-1.0)   order-service
     order_service_memory_bytes     (gauge, bytes)     order-service
     checkout_timeout_rate          (gauge, 0.0-1.0)   checkout-service
     inventory_check_p95_seconds    (gauge, seconds)   inventory-service
     payment_gateway_error_rate     (gauge, 0.0-1.0)   payment-gateway
     payment_declines_total         (counter)          payment-gateway

     Exposed for scraping completeness, never observed (no simulated traffic):
     order_request_duration_seconds (histogram)
     order_requests_total           (counter)

POST /chaos/order-failures      {"rate": 0.15, "duration_seconds": 120}
POST /chaos/checkout-timeouts   {"rate": 0.35, "duration_seconds": 120}
POST /chaos/payment-errors      {"rate": 0.15, "duration_seconds": 120}
POST /chaos/inventory-latency   {"value": 1.5, "duration_seconds": 120}
POST /chaos/order-memory        {"value": 2.5e9, "duration_seconds": 300}
POST /chaos/payment-declines    {"per_second": 10.0, "duration_seconds": 300}

POST /chaos/reset
     Clears every scenario. Gauges return to baseline immediately; the decline
     counter stops climbing but is NOT rewound (a counter going backwards means
     "process restarted" to rate(), which would corrupt the rule's own query).

GET  /healthz -> 200
```

Three request shapes, because the metric kinds validate differently:

- **ratio gauges** take `rate` (0.0-1.0). The `le=1.0` bound is real validation:
  it rejects `{"rate": 15}` from someone who meant 15%, which would otherwise pin
  the gauge at 15.0 and breach every ratio rule at once while returning 200.
- **absolute gauges** take `value` in the metric's own unit (seconds, bytes),
  deliberately uncapped — 1.5s and 2.5e9 bytes are both ordinary.
- **counters** take `per_second`. A counter cannot be pinned: the rule reads
  `rate()`, the slope, so the metric must EVOLVE. Each scrape advances it by
  `per_second x elapsed` within the active window, carrying the fractional
  remainder so it advances by whole events without losing any to rounding.

No background reset task anywhere: a spike stores a monotonic deadline and the
metric is computed from it at scrape time, so expiry IS the reset.

### Alert rules

Live in `deploy/prometheus/alerting-rules.yml`. These are the **target-stack**
rules: they describe a made-up shop that exists to generate incidents for RADAR to
work on. RADAR's own **service-health** alerts (LLM gateway fallback, outbox
backlog, agent health) are a separate Phase 10 deliverable and land beside them
under `deploy/prometheus/` in their own file. The distinction is what each set
watches — the simulated shop versus RADAR itself — not which repository holds it;
there is only one repository (ADR 0018). Six rules across four services:

```
order-service       OrderProcessingFailureRate  > 0.05        for 1m   critical
order-service       OrderServiceHighMemory      > 1.5e9       for 5m   medium
checkout-service    CheckoutTimeoutRate         > 0.10        for 2m   high
inventory-service   InventoryCheckLatency       > 0.5         for 2m   high
payment-gateway     PaymentGatewayErrorRate     > 0.05        for 1m   critical
payment-gateway     PaymentDeclineRate          rate[2m] > 2  for 2m   medium
```

`severity` must come from the canonical `Severity` enum
(critical|high|medium|low|info). Prometheus's conventional `warning`/`page`
spellings are rejected by ingestion with 422 — it validates against that set and
never translates one spelling to another.

A rule has TWO bars and a spike must clear both: magnitude, and duration. A spike
shorter than the rule's `for` never fires however large it is. The measured
minimum spike and duration per rule are tabulated in the rules file header;
`PaymentDeclineRate` is the one to watch, because `rate()` over a range climbs as
the window fills, so the window ADDS to `for` rather than overlapping it.

### Not wired up yet

Nothing mounts or evaluates the rules file: there is no scrape config and no
alertmanager in `deploy/compose/docker-compose.yml`, and platform-sim is not a
compose service. Standing up the running Prometheus + alertmanager, wiring them
into compose, and proving the real front door in the default suite are all
**deferred to Phase 10 (Observability)**, which already owns that infrastructure.

The real path is nonetheless proven ONCE, opt-in, by
`tests/e2e/test_real_prometheus_alert.py` under the `infra` marker: real
Prometheus with these rules mounted, real alertmanager, firing to a real webhook
receiver in ~85s. It is deselected by default so the suite never depends on
Docker.

## Knowledge Service

### Runbook Format

```yaml
---
id: runbook-001
title: Order Service High Failure Rate
severity: critical
services:
  - order-service
symptoms:
  - order_processing_failure_rate above 5% for more than 1 minute
  - downstream checkout-service reporting 500s from order-service
---

## Overview
High failure rates in order-service typically indicate either a bad deployment,
database connectivity issues, or upstream dependency failures.

## Common Causes
- Recent deployment with a bug in order validation or persistence layer
- order-db connection pool exhaustion under traffic spike
- payment-gateway returning errors causing order completion failures

## Investigation Steps
1. Check recent deployments: kubectl rollout history deployment/order-service
2. Check pod logs: kubectl logs -l app=order-service --since=30m | grep ERROR
3. Check DB connections: review order-db connection pool metrics in Grafana
4. Check payment-gateway: curl -s http://payment-gateway/healthz
5. Check error breakdown by type in Kibana

## Resolution
- Bad deployment: kubectl rollout undo deployment/order-service
- DB pool: increase pool size in ConfigMap, rolling restart
- Payment gateway: escalate to payments team, check circuit breaker state

## Prevention
- Add post-deploy smoke tests that validate order_processing_failure_rate
- Alert on DB connection pool at 70% before saturation hits
```

### Elasticsearch Index

```json
{
  "index": "radar-runbooks",
  "mappings": {
    "properties": {
      "runbook_id":  {"type": "keyword"},
      "title":       {"type": "text", "analyzer": "english"},
      "services":    {"type": "keyword"},
      "severity":    {"type": "keyword"},
      "chunk_index": {"type": "integer"},
      "content":     {"type": "text", "analyzer": "english"},
      "embedding":   {
        "type": "dense_vector",
        "dims": 1536,
        "index": true,
        "similarity": "cosine"
      }
    }
  }
}
```

### Retrieval Strategy

```
1. Build query string from service_name + alert_name + investigation_steps
2. Pre-filter: only chunks where services contains service_name
3. BM25 keyword search -> top 20
4. kNN vector search -> top 20
5. Merge with RRF -> top 10
6. Cross-encoder rerank -> top 5
7. CRAG grade each chunk via llm-gateway (mode=reason)
8. Return all 5 with grades
9. Reasoner uses: sufficient and partial. Skips: insufficient.
```

---

## Observability

### Required Metrics (all services)

```
radar_requests_total{service, endpoint, status_code}      counter
radar_request_duration_seconds{service, endpoint}         histogram
radar_errors_total{service, error_type}                   counter
```

### LLM Gateway Metrics

```
radar_llm_requests_total{mode, provider, status}          counter
radar_llm_duration_seconds{mode, provider}                histogram
radar_llm_time_to_first_token_seconds{mode, provider}     histogram
radar_llm_tokens_total{mode, provider, direction}         counter
radar_llm_provider_errors_total{mode, provider, error}    counter
radar_llm_fallback_total{from_provider, to_provider}      counter
radar_llm_template_fallback_total                         counter
```

### Outbox Metrics

```
radar_outbox_depth                                        gauge
radar_outbox_processing                                   gauge
radar_outbox_dead_letter_total                            counter
radar_outbox_dispatch_duration_seconds{target_service}    histogram
radar_outbox_retry_total{event_type}                      counter
```

### Incident Pipeline Metrics

```
radar_incidents_total{service, severity}                  counter
radar_incident_duration_seconds                           histogram
radar_recommendations_total{provider, confidence}         counter
radar_recommendations_fallback_total                      counter
radar_feedback_total{sentiment}                           counter
```

### Prometheus Alert Rules

```yaml
groups:
  - name: radar-platform
    rules:
      - alert: OutboxDepthHigh
        expr: radar_outbox_depth > 50
        for: 5m
        annotations:
          summary: "Outbox queue is backing up, pipeline may be stalling"

      - alert: OutboxDeadLetterHigh
        expr: increase(radar_outbox_dead_letter_total[10m]) > 5
        annotations:
          summary: "Dead letter queue growing, check outbox worker logs"

      - alert: LLMProviderErrorRate
        expr: rate(radar_llm_provider_errors_total[5m]) > 0.1
        annotations:
          summary: "LLM provider error rate above 10%"

      - alert: LLMFallbackRateHigh
        expr: rate(radar_llm_template_fallback_total[15m]) > 0
        annotations:
          summary: "RADAR is using template fallbacks, LLM providers may be down"

      - alert: IncidentRCAStalled
        expr: increase(radar_incidents_total[10m]) > 0
          unless increase(radar_recommendations_total[10m]) > 0
        annotations:
          summary: "Incidents are being created but no recommendations are being written"
```

### Grafana Dashboards

```
radar-overview.json      Request rate, error rate, p50/p95/p99 per service
llm-gateway.json         Mode usage, provider breakdown, latency, tokens, fallback rate
outbox-health.json       Depth, in-flight, dead letter, dispatch duration
incident-pipeline.json   Incidents/hour, alert-to-recommendation latency, fallback rate
feedback-quality.json    Positive/negative ratio, correction rate over time
```

---

## Engineering Standards

### Package Versions

# NOTE: Some pins below were bumped for Python 3.14 wheel compatibility (pydantic 2.10.0->2.13.4, ruff 0.8.0->0.15.20, mypy 1.13.0->2.1.0); remaining packages to be re-verified for 3.14 as each phase lands.

```toml
[project]
requires-python = ">=3.12"

[tool.uv.dependencies]
fastapi                              = "0.115.0"
uvicorn                              = {extras = ["standard"], version = "0.32.0"}
pydantic                             = "2.13.4"
pydantic-settings                    = "2.6.0"
sqlalchemy                           = {extras = ["asyncio"], version = "2.0.36"}
alembic                              = "1.14.0"
asyncpg                              = "0.30.0"
anthropic                            = "0.40.0"
openai                               = "1.55.0"
google-generativeai                  = "0.8.3"
structlog                            = "24.4.0"
prometheus-client                    = "0.21.0"
opentelemetry-sdk                    = "1.28.0"
opentelemetry-exporter-otlp-proto-grpc = "1.28.0"
opentelemetry-instrumentation-fastapi = "0.49b0"
httpx                                = "0.28.0"
pyyaml                               = "6.0.2"
slack-sdk                            = "3.33.0"

[tool.uv.dev-dependencies]
pytest          = "8.3.0"
pytest-asyncio  = "0.24.0"
pytest-cov      = "6.0.0"
ruff            = "0.15.20"
mypy            = "2.1.0"
```

### Every Service Must Have

```
GET /healthz     200 if process alive
GET /readyz      200 if DB reachable and Vault secrets loaded. 503 otherwise.
GET /metrics     Prometheus text format
POST /events     Agents only. Not llm-gateway.
structlog JSON to stdout
correlation_id on every log line
OTel span per request
Timeout on every outbound HTTP call
Bounded retries on outbound calls
X-Radar-Agent-Token on all non-health non-metrics endpoints
SIGTERM handler: drain, exit within 30s
```

### Dockerfile Pattern

```dockerfile
FROM python:3.12-slim

WORKDIR /app

RUN pip install uv --no-cache-dir

COPY pyproject.toml .
RUN uv pip install --system -e . --no-cache

COPY src/ src/

RUN useradd --system --uid 1001 radar
USER radar

EXPOSE 8080

CMD ["uvicorn", "radar_SERVICE.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
```

### Vault Init-Container Pattern

```yaml
initContainers:
  - name: vault-init
    image: hashicorp/vault:1.18
    env:
      - name: VAULT_ADDR
        value: "http://vault.radar-infra.svc.cluster.local:8200"
    command:
      - sh
      - -c
      - |
        vault login -method=kubernetes role=radar-SERVICE
        vault kv get -field=agent_token secret/radar/SERVICE > /vault/secrets/agent_token
        vault kv get -field=openai_api_key secret/radar/llm > /vault/secrets/openai_api_key
    volumeMounts:
      - name: vault-secrets
        mountPath: /vault/secrets
volumes:
  - name: vault-secrets
    emptyDir:
      medium: Memory
```

---

# Implementation Phases

Each phase = one PR. One milestone. Commit history tells the story of how the project grew.
No dump commits. No "add everything" PRs.

---

## Phase 0: Foundation
**Milestone: v0.0-foundation**

Zero code. Docs and decisions only.

Deliverables:
```
README.md (vision, problem, non-goals)
LICENSE
CONTRIBUTING.md
.gitignore
docs/adr/ (all 12 ADRs)
docs/architecture/system-overview.md
docs/architecture/agent-pipeline.md
docs/architecture/data-model.md
docs/architecture/sequence-flows.md
docs/glossary.md
docs/roadmap.md
```

Commits:
```
chore: initialize radar-system repository
docs: add product vision goals and non-goals
docs: add system architecture and agent pipeline design
docs: add data model and sequence flows
docs: add all architectural decision records
docs: add glossary and roadmap
```

Done when: a reader understands what RADAR is, what it is not, and why every major
decision was made. No implementation ambiguity.

---

## Phase 1: Developer Environment
**Milestone: v0.1-dev-env**

Deliverables:
```
pyproject.toml (workspace root)
uv.lock
Makefile
.env.example
.pre-commit-config.yaml
scripts/bootstrap.sh
deploy/compose/docker-compose.yml
```

Compose stack:
```
postgres:16
elasticsearch:8.16.0
kibana:8.16.0
prometheus:v2.55.0
grafana:11.3.0
vault:1.18.0
```

.env.example:
```
POSTGRES_DSN=postgresql+asyncpg://radar:radar@localhost:5432/radar
ELASTICSEARCH_URL=http://localhost:9200
VAULT_ADDR=http://localhost:8200
VAULT_DEV_ROOT_TOKEN=dev-root-token
OPENAI_API_KEY=sk-...
SLACK_BOT_TOKEN=xoxb-...
SLACK_APP_TOKEN=xapp-...
```

Makefile:
```
make setup    uv sync, pre-commit install
make dev      docker compose up -d
make stop     docker compose down
make lint     ruff check . && mypy .
make test     pytest
make clean    docker compose down -v
```

Commits:
```
chore: add python 3.12 workspace with uv
chore: add ruff mypy and pre-commit config
chore: add makefile with dev environment targets
chore: add docker compose local stack
docs: add local development setup guide
```

Done when: make setup && make dev works on a clean machine and all six services are reachable.

---

## Phase 2: Contracts and Plugin SDK
**Milestone: v0.2-contracts**

Deliverables:
```
packages/contracts/
packages/plugin-sdk/
```

All models Pydantic v2. All backends Protocol interfaces. Zero vendor imports.

Contracts to define:
```
NormalizedAlert, Incident, OutboxEvent, ProcessedEvent
InvestigationPlan, Recommendation, FeedbackEvent
LLMRequest, LLMResponse, GatewayStreamEvent
BotCommand, BotResponse
LogsBackend, MetricsBackend, TracesBackend
NotificationBackend, LLMProvider, EmbeddingProvider, KnowledgeStore
```

Commits:
```
feat(contracts): add alert incident and outbox event schemas
feat(contracts): add plan recommendation and feedback schemas
feat(contracts): add llm request response and stream schemas
feat(contracts): add slack bot command and response schemas
feat(contracts): add backend protocol interfaces
feat(plugin-sdk): add plugin registry with protocol conformance check
feat(plugin-sdk): add config-driven backend loader
test(contracts): add schema validation and serialization tests
test(plugin-sdk): add registry conformance tests
```

Done when: mypy strict passes. Zero vendor imports in contracts or plugin-sdk.

---

## Phase 3: Shared Packages
**Milestone: v0.3-packages**

Deliverables:
```
packages/common/
packages/database/
packages/telemetry/
```

Three tests you must write explicitly:

Test 1: Outbox atomicity
```
Insert incident + outbox event in a transaction
Raise an exception before commit
Verify neither record exists in DB
```

Test 2: Concurrent poller isolation
```
Insert 10 outbox events
Run two pollers simultaneously
Verify each event claimed by exactly one poller
```

Test 3: Idempotency
```
Mark event_id as processed for service X
Call is_already_processed again with same event_id + service X
Verify returns True and second processing is skipped
```

Commits:
```
feat(common): add structured logging with correlation id
feat(common): add config loader with vault secret file support
feat(common): add agent token auth fastapi dependency
feat(common): add error classes and id helpers
feat(database): add async postgres engine and session factory
feat(database): add all sqlalchemy models
feat(database): add transactional outbox writer
feat(database): add repository layer per model
feat(database): add alembic with initial migration
feat(telemetry): add prometheus metric factories
feat(telemetry): add otel tracer with otlp elasticsearch exporter
test(database): add outbox atomicity test
test(database): add concurrent poller isolation test
test(database): add processed event idempotency test
test(common): add auth token validation tests
```

Done when: all three named tests pass. Migrations run clean on a fresh Postgres.

---

## Phase 4: LLM Gateway
**Milestone: v0.4-llm-gateway**

Deliverables:
```
apps/llm-gateway/
plugins/llm/anthropic/
plugins/llm/openai/
plugins/llm/gemini/
```

This is the most critical service. Get it right the first time.

Implement everything in the LLM Gateway specification: mode config, token IAM,
request validation, per-mode timeout, retry, fallback provider, streaming,
embed endpoint, all metrics, OTel tracing, no prompt logging.

Commits:
```
feat(llm-gateway): add service skeleton with health readyz metrics
feat(llm-gateway): add mode config with per-mode model token limits timeout
feat(llm-gateway): add agent token iam with mode enforcement
feat(llm-gateway): add request validation pipeline
feat(plugin-llm-openai): add openai sdk provider
feat(plugin-llm-anthropic): add anthropic sdk provider
feat(plugin-llm-gemini): add gemini sdk provider
feat(llm-gateway): add model router selecting provider by mode
feat(llm-gateway): add complete endpoint
feat(llm-gateway): add streaming support
feat(llm-gateway): add embed endpoint
feat(llm-gateway): add retry timeout and fallback provider
feat(llm-gateway): add prometheus metrics
feat(llm-gateway): add otel tracing per llm call
test(llm-gateway): add mode enforcement tests
test(llm-gateway): add token validation tests
test(llm-gateway): add retry and fallback tests
test(llm-gateway): add provider adapter unit tests
```

Done when:
- Wrong token -> 401
- Wrong mode -> 403
- Over token limit -> 422
- Real OpenAI call works with key from .env
- Provider failure triggers fallback, not a crash

---

## Phase 5: Ingestion and Platform Simulator
**Milestone: v0.5-ingestion**

Deliverables:
```
apps/ingestion/
apps/platform-sim/
deploy/prometheus/alerting-rules.yml
plugins/logs/elastic/
plugins/metrics/prometheus/
```

Ingestion endpoints:
```
POST /alerts/prometheus
POST /alerts/kibana
POST /alerts/mock
GET  /healthz
GET  /readyz
GET  /metrics
```

External sources use X-Radar-Webhook-Token (not the internal agent token).
Configured per source. Document in ADR 0011.

Dedup logic:
```
fingerprint = sha256(service_name + ":" + alert_name + ":" + severity)
Query: open incident with same fingerprint within 5 minutes
If found: attach alert, no new outbox event
If new: INSERT incident + outbox_event in one transaction
```

Commits:
```
feat(order-stub): add order service metrics and chaos endpoints
feat(ingestion): add alert ingestion api with source routing
feat(ingestion): add vendor-neutral normalizer per source
feat(ingestion): add fingerprint deduplication
feat(ingestion): add transactional incident creation and outbox publish
feat(ingestion): add inbound webhook token validation
feat(plugin-logs-elastic): add elasticsearch log backend
feat(plugin-metrics-prometheus): add prometheus metrics backend
test(ingestion): add normalizer tests per source type
test(ingestion): add deduplication boundary tests
test(ingestion): add atomicity test
docs: add adr 0011 inbound webhook token pattern
```

Done when: POST /alerts/mock creates one incident + outbox event. Second identical
POST within 5 minutes creates neither.

### Pre-Phase-8 extension (done later, on `pre-phase-8-order-stub-fix`)

Phase 5 shipped a stub simulating order-service only, and MET its bar — the
done-condition above is written entirely in terms of `/alerts/mock`. But Phase 8
retrieval is triggered by incidents, and incidents come from alerts, so a stub
that fires three scenarios would leave most of a 15-20 runbook corpus describing
alerts nothing can produce. The stub was therefore extended into the platform
simulator BEFORE writing any runbook, so every Tier-1 runbook has a matching
fireable alert.

```
refactor(order-stub): rename the stub to platform-simulator
feat(deploy): declare prometheus alert rules for simulated scenarios
feat(platform-sim): add payment-gateway error-rate and decline-rate scenarios
feat(platform-sim): add inventory-service check-latency scenario
feat(platform-sim): add order-service memory-pressure scenario
test(platform-sim): prove a chaos spike drives an alert to an incident
```

Scope decision, so it is not relitigated: the alert RULES are declared here, in
`deploy/prometheus/alerting-rules.yml`. The running Prometheus + alertmanager,
their compose wiring, and proving the real scrape -> fire -> webhook path in the
default suite are **Phase 10's**, not Phase 5's — that infrastructure was always
a Phase 10 deliverable, and Phase 8 does not need it (ingestion creates the same
incident whether the alert arrived from a real breach or a crafted POST, and the
reasoner retrieves on service_name + alert_name either way). The real path is
proven once, opt-in, behind the `infra` marker.

Extended done-condition: a chaos spike breaches its declared rule and the
resulting alertmanager-shaped body creates exactly one incident through
`/alerts/prometheus`; a second identical POST creates none. Six scenarios across
four services are fireable.

---

## Phase 6: Outbox Worker
**Milestone: v0.6-outbox-worker**

Deliverables:
```
apps/outbox-worker/
```

Implement the full spec. Every single part of it.

Commits:
```
feat(outbox-worker): add polling loop with select for update skip locked
feat(outbox-worker): add http dispatcher with 10s hard timeout
feat(outbox-worker): add exponential backoff retry with process_after scheduling
feat(outbox-worker): add dead letter promotion and audit log write
feat(outbox-worker): add graceful shutdown with 30s drain
feat(outbox-worker): add admin dead letter list and requeue endpoints
feat(outbox-worker): add prometheus metrics
test(outbox-worker): add concurrent polling no-duplicate test
test(outbox-worker): add dead letter promotion test
test(outbox-worker): add idempotency on restart test
test(outbox-worker): add graceful shutdown test
```

Done when: two workers running simultaneously never double-process the same event.
Proven by test.

---

## Phase 7: Agent Pipeline and Vertical Slice
**Milestone: v0.7-vertical-slice | Tag: v0.1.0**

Deliverables:
```
apps/watcher-agent/    (with config/correlation-rules.yaml)
apps/planner-agent/    (with config/plan-templates.yaml)
apps/reasoner-agent/   (with fallback.py)
tests/e2e/test_vertical_slice.py
```

v1 constraints:
- Watcher: YAML correlation rules, no LLM
- Planner: YAML template matching, no LLM
- Reasoner: LLM call + template fallback if LLM fails

E2E test:
```python
async def test_mock_alert_to_recommendation():
    response = await client.post("/alerts/mock", json=order_service_alert)
    assert response.status_code == 202
    incident_id = response.json()["incident_id"]

    recommendation = await poll_until(
        lambda: db.get_recommendation_for_incident(incident_id),
        timeout_seconds=60
    )

    assert recommendation.root_cause is not None
    assert recommendation.confidence in ("low", "medium", "high")
    assert len(recommendation.recommended_actions) >= 1

    incident = await db.get_incident(incident_id)
    plan = await db.get_plan_for_incident(incident_id)
    assert incident.correlation_id == plan.correlation_id == recommendation.correlation_id

async def test_deduplication():
    await client.post("/alerts/mock", json=order_service_alert)
    await client.post("/alerts/mock", json=order_service_alert)
    incidents = await db.get_incidents_by_fingerprint(fingerprint)
    assert len(incidents) == 1

async def test_fallback_on_llm_failure():
    # Mock llm-gateway to return 503
    recommendation = await run_pipeline_with_failed_llm()
    assert recommendation.is_fallback is True
    assert recommendation.confidence == "low"
    assert len(recommendation.recommended_actions) >= 1
```

Commits:
```
feat(watcher): add correlation rules loader
feat(watcher): add fingerprint correlation with service groups
feat(watcher): add suppression and escalation enforcement
feat(watcher): add incident creation and outbox trigger
feat(planner): add plan template loader
feat(planner): add template matching with default fallback
feat(planner): add plan storage and outbox trigger
feat(reasoner): add context bundle builder
feat(reasoner): add llm gateway call with extended mode
feat(reasoner): add template fallback rca on llm failure
feat(reasoner): add rca json parser with validation
feat(reasoner): add recommendation storage and outbox trigger
test(watcher): add correlation window tests
test(watcher): add service group tests
test(watcher): add escalation tests
test(planner): add template matching tests
test(reasoner): add context bundle tests
test(reasoner): add fallback rca tests
test(e2e): add vertical slice test
test(e2e): add deduplication test
test(e2e): add llm fallback test
```

Done when: all three e2e tests pass with real OpenAI calls. Tag v0.1.0.
This is your POC. Everything after this is improvement.

---

## Phase 8: Knowledge Service
**Milestone: v0.8-knowledge | Tag: v0.2.0**

Write the runbooks before writing code. You need real content to test retrieval against.

Every Tier-1 runbook below has a matching FIREABLE alert: platform-sim was
extended before this phase precisely so none of them describes an alert nothing
can produce (see the Phase 5 pre-Phase-8 extension). The mapping is one-to-one:

```
order-service-high-failure-rate  <- OrderProcessingFailureRate  order-service
order-service-high-memory        <- OrderServiceHighMemory      order-service
checkout-timeout-rate            <- CheckoutTimeoutRate         checkout-service
inventory-check-latency          <- InventoryCheckLatency       inventory-service
payment-gateway-errors           <- PaymentGatewayErrorRate     payment-gateway
payment-decline-rate             <- PaymentDeclineRate          payment-gateway
```

Retrieval is triggered by an incident and matches on service_name + alert_name,
so a runbook whose alert cannot fire is corpus no e2e will ever retrieve. Drive
these end to end with the chaos endpoints, not with hand-written incidents.

Deliverables:
```
docs/runbooks/                      # 17 runbooks as built: the 6 Tier-1 below
                                    # plus 11 depth runbooks (see its README)
apps/knowledge-service/             # package: radar_knowledge_service
plugins/knowledge/elastic/          # the dense-vector index + search primitives
plugins/traces/elastic/             # DEFERRED to Phase 10 — see below
```

`plugins/traces/elastic/` is **deferred to Phase 10**, for the same reason
Prometheus/alertmanager wiring was: it is an OTel traces backend, and everything
that would consume it — the collector, Fluent Bit, the tracing path, the
dashboards — lands in Phase 10. Nothing in Phase 8 references it, and its
done-condition does not depend on it. Building it here would have added a
component with no consumers to tick a list item.

Commits (planned, with as-built names where they differ):
```
docs(runbooks): add order service high failure rate runbook
    -> shipped as "add runbook frontmatter contract and order service
       failure rate runbook" (the contract landed with the first runbook)
docs(runbooks): add order service high memory runbook
docs(runbooks): add checkout timeout rate runbook
docs(runbooks): add inventory latency runbook
docs(runbooks): add payment gateway errors runbook
docs(runbooks): add payment decline rate runbook
    + 4 unplanned: "add {order,checkout,inventory,payment} ... depth runbooks"
      — the 11 depth runbooks, so retrieval must disambiguate WITHIN a service
feat(knowledge): add runbook indexer with sha256 change detection
    + "add runbook chunker with content-addressed chunk ids" and
      "add incremental indexing reconciliation" — the pure cores the
      indexer is a shell over
feat(knowledge): add elasticsearch dense vector index setup
    -> shipped under feat(plugin-knowledge-elastic): the mapping lives in
       the plugin, not the service
feat(knowledge): add embedding calls via llm-gateway embed mode
feat(knowledge): add hybrid bm25 and knn retrieval with rrf
    -> three commits on the pure-core/thin-shell seam: the fusion + query
       core, feat(plugin-knowledge-elastic) search primitives, and the
       composition
feat(knowledge): add crag grading via llm-gateway reason mode
    -> two commits: the pure core, then the gateway call + wiring
feat(knowledge): add context api for reasoner
feat(plugin-traces-elastic): add otel traces elasticsearch backend   [-> Phase 10]
feat(reasoner): upgrade to v2 context bundle with knowledge retrieval
feat(reasoner): update system prompt to reference retrieved context
test(knowledge): add retrieval tests against known runbook content
    -> the pre-registered probe set and per-stage baselines under
       tests/retrieval/, plus each module's own suite
test(knowledge): add crag grading tests
    -> apps/knowledge-service/tests/test_crag*.py, plus the empty-context
       e2e that gated the stage
test(e2e): add knowledge-assisted rca test
```

### Added during implementation

Commits this phase produced that the list above does not name. Recorded here
because the plan is the source of truth, and a phase whose real history diverges
from its stated deliverables should say so rather than let the gap accumulate
silently.

```
test(knowledge): prove incremental pickup end to end on real infrastructure
feat(knowledge): stamp indexed chunks with a per-run timestamp
test(knowledge): pre-register retrieval probes and record the pre-rerank baseline
docs(knowledge): pre-register the three-bucket attribution model for retrieval
feat(knowledge): grant knowledge-service a second gateway token for reason mode
test(knowledge): pre-register the rank metric and its stability floor
```

Why each was needed:

- **The incremental-pickup e2e** proves the phase's central claim — adding a
  runbook re-embeds only that runbook, and the new content is then retrievable —
  on real Elasticsearch, gateway, OpenAI, and Postgres together. The planned
  `test(e2e): add knowledge-assisted rca test` covers the reasoner using
  retrieved content, which is a different claim.
- **The per-run `indexed_at` stamp** makes "which chunks did run N write" a
  single-term query. Per-runbook would only restate `runbook_documents.indexed_at`,
  which Postgres already answers.
- **The three pre-registration commits** (probes, attribution model, rank metric)
  exist so `feat(knowledge): add cross-encoder reranking` can be judged rather
  than assumed. Reranking is the phase's most expensive stage; without margins
  and ranks recorded BEFORE it exists, "rerank improved retrieval" is a claim
  with no baseline to test it against, and the stage cannot be told apart from
  the pre-filter or from RRF. The planned
  `test(knowledge): add retrieval tests against known runbook content` tests that
  retrieval works; this measures what each stage contributes.
- **The second gateway token** is a hard prerequisite: reranking calls the
  gateway in `reason` mode, the service held only an `embed` grant, and "one
  token = one mode" is a Locked Decision — so it needed a second token, not a
  widened one.

### `feat(knowledge): add cross-encoder reranking` — implemented, then REMOVED

The deliverable was built in full (pure core, gateway client, wiring), measured
against a criterion pre-registered before the stage existed, and then removed.
The evidence is checked in: `tests/retrieval/probes.yaml` holds the criterion and
the predictions, `tests/retrieval/baseline-reranked.json` holds the result with
`criterion_met: false`.

At 20 repeats per probe, reranking:

1. **did not reliably fix either target** — the depth case reached rank 1 in 9
   runs of 20, the repair case in 16 of 20;
2. **was the pipeline's only source of run-to-run variance** — filter, kNN and
   RRF return identical ranks on all 17 probes at n=20;
3. **destabilised a probe** that had been rank 1 at every earlier stage;
4. **cost a `reason`-mode LLM call on every incident.**

It improved the average, which is not the same as improving the system. An
on-call engineer sees one retrieval, not a distribution, so 16-in-20 means one
incident in five grounds the RCA in the wrong runbook — and differently on
different days for the same alert. Deterministic and slightly worse beats better
on average but unpredictable, when each incident is a single draw and the result
has to be debuggable afterwards.

Retrieval is therefore **filter -> BM25 + kNN -> RRF -> CRAG**, with no rerank
step, and the plan's retrieval strategy (step 6) does not describe what is built.
`retrieval.py` carries the same summary where the stage used to be, so the
absence reads as a finding rather than an oversight.

An honest note on what this cost: the stage was built before it was measured,
because the pre-registered criterion needed something to measure. That order was
deliberate and the work was not wasted — the negative result is only credible
because the implementation was a real one — but it does mean a full slice of
code was written, proven, and deleted.

`feat(knowledge): add hybrid bm25 and knn retrieval with rrf` was implemented as
three commits rather than one, split on the pure-core/thin-shell seam this
codebase uses elsewhere: the fusion and query-assembly core (pure, mutation
tested), the Elasticsearch search primitives (I/O, tested against real ES), and
the wiring that composes them.

Additional deliverables, as built:
```
apps/knowledge-service/src/radar_knowledge_service/
    chunking.py reconciliation.py indexer.py embeddings.py   # indexing
    query.py fusion.py retrieval.py                          # retrieval core
    crag.py crag_client.py                                   # grading
    api.py main.py config.py                                 # the context API
    index.py                                                 # `make index`
apps/reasoner-agent/src/radar_reasoner_agent/knowledge.py    # the v2 client
tests/retrieval/probes.yaml          # pre-registered queries + success criteria
tests/retrieval/baseline*.json       # per-stage margins, ranks, stability floors
scripts/measure-retrieval-baseline.py
scripts/measure-retrieval-stages.py
tests/e2e/test_incremental_indexing.py
tests/e2e/test_crag_empty_context.py
tests/e2e/test_knowledge_assisted_rca.py
```

Three items listed here as outstanding during the phase were closed before it
ended: the indexer entrypoint (`make index`), `status: fixture` carried through
to the context API, and the e2e gateway-port reconciliation — whose fix also
made an explicitly-set-but-unreachable `RADAR_GATEWAY_URL` FAIL rather than skip,
since a skip there reads as "opted out" when the truth is "misconfigured".

One limitation is recorded rather than fixed, and pre-registered in
`docs/roadmap.md`: no-coverage detection is reliable for symptom-rich queries but
boundary-unstable for the alert-shaped query an UNKNOWN alert produces, because
the planner's `_default` steps dominate that query and their generic language
("review latency trends") grazes runbook content. Measured 2/5 empty. The fix is
query quality, not grader tuning.

Done when: RCA for an order-service alert references content from the order-service runbook.
Verify by reading the recommendation row in Postgres manually.

---

## Phase 9: Feedback Service and Slack Bot
**Milestone: v0.9-feedback | Tag: v0.3.0**

Deliverables:
```
apps/feedback-service/    (RCA delivery + bot)
plugins/notifications/slack/
```

Both features live in feedback-service. One deployment. One Slack connection.

Commits:
```
feat(plugin-slack): add slack notification backend
feat(feedback): add rca delivery card formatter
feat(feedback): add slack message delivery on recommendation.created event
feat(feedback): add slack interactive callback handler for thumbs up/down
feat(feedback): create feedback record linked to recommendation
feat(feedback): update incident status on feedback received
feat(feedback): emit feedback metrics
feat(feedback): add slack bot event handler for app_mention
feat(feedback): add bot command parser
feat(feedback): add status command handler
feat(feedback): add open incidents command handler
feat(feedback): add incident detail command handler
feat(feedback): add last n incidents command handler
feat(feedback): add daily summary command handler
test(feedback): add rca card formatting tests
test(feedback): add slack callback handling tests
test(feedback): add bot command parser tests
test(feedback): add bot query handler tests
```

Done when: POST mock alert -> Slack RCA card appears -> thumbs up creates feedback row.
@radar open returns list of open incidents in Slack.

### Footprint divergence: stage 1 touches ingestion and packages/database

Recorded because the plan is the source of truth, and a phase whose real history
diverges from its stated footprint should say so rather than let the gap
accumulate silently (same discipline as Phase 8's "Added during implementation").

Both the "Git State Per Phase" line for Phase 9 (`+ apps/feedback-service
plugins/notifications`) and the commit list above are wrong about this phase's
footprint. They describe Phase 9 as feedback-service and the Slack plugin only.
Stage 1 — the ingestion-side incident lifecycle — is entirely in `apps/ingestion`
and `packages/database`, with no feedback-service and no Slack:

```
fix(ingestion): stop resolved alerts from opening incidents
feat(database): add validated incident status transitions with audit log
feat(ingestion): mark alerts resolved on alertmanager resolved webhook
feat(ingestion): resolve incidents when their last firing alert resolves
```

Why it belongs here and not in an earlier phase: closure is a lifecycle
guarantee, and the build order deliberately proves the lifecycle end to end —
Alertmanager `resolved` webhook -> alerts flip -> incident resolves — BEFORE any
Slack surface exists, so the closure path is provable without a bot. Pulling it
into Phase 5 (ingestion) would have built incident *resolution* before incidents
had a downstream that cares; deferring it into the Slack work would have tangled a
database state machine with bot wiring. The state-machine helper lives in
`packages/database` because feedback-service needs the identical transition logic
in stages 3–4 (engineer Slack action, `@radar close`), and one enforced state
machine must not become two.

The ADR 0016 amendments this stage required (feedback-service owns
`open -> investigating`; ingestion's authority widened to `{open, investigating}
-> resolved`; `closed_at` reserved) are recorded inline in ADR 0016 above.

### What shipped

Phase 9 was built in five stages plus a feedback-metrics close-out. Both
done-conditions hold:

1. **mock alert -> RCA card -> 👍 creates a feedback row** — stages 2–3.
2. **`@radar open` returns open incidents in Slack** — stage 4, proven end to end
   through the wired Socket Mode listener, not just the parser unit.

- **Stage 1 — incident lifecycle** (ingestion + `packages/database`): the validated
  `transition_status` state machine with its audit log, and the Alertmanager
  `resolved` path that flips alerts and resolves an incident when its last firing
  alert clears. Built before any Slack surface so closure is provable without a bot
  (see the footprint note above).
- **Stage 2 — RCA delivery**: the Slack notification backend, the RCA card
  formatter, at-least-once delivery on `recommendation.created` (post then record,
  one card under a row lock held across the post), and the `open -> investigating`
  transition gated on delivery (ADR 0016 Amendment 1).
- **Stage 3 — interactive callbacks**: the neutral `NotificationInteraction`
  contract, the Socket Mode source + `chat.update`, the strict callback parser
  (deep-treatment — it writes against the wrong recommendation or resolves the wrong
  incident if it mis-parses), and the handler that writes 👍/👎 rows or resolves the
  incident, the concurrent-resolve loser recording the forensic
  `incident.invalid_transition` audit and returning benignly.
- **feedback-metrics**: `radar_feedback_total{sentiment}`, incremented after the row
  commits so the counter counts recorded feedback, never attempted.
- **Stage 4 — the `@radar` bot**: the neutral `BotMention` contract, `app_mention`
  received over the same socket, the command parser (the one parse surface — bot
  handle stripped first, closed verb set, `<id>` validated at parse), the read
  queries as repository methods in `packages/database`, and the atomic wire-up that
  turns the bot on: parse -> query -> in-thread reply, with the `bot_max_rows` cap
  enforced at the handler.

### Deferred, with reasons

- **The correction modal** (a 📝 on the RCA card capturing a human's fix) — the
  consumer that re-reasons over a correction does not exist yet, so a captured
  correction would land in a `feedback` row nobody reads. Deferred until something
  acts on it; `correction_text` stays reserved on the schema and the contract.
- **`@radar close`** (`resolved -> closed`) — the state machine has the edge and
  `transition_status` stamps `closed_at`, but no caller reaches `closed` (ADR 0016
  Amendment 3). The bot is read-only in v1; a state-changing command is a different
  rigor tier and lands when close is actually wanted.
- **True ephemeral bot replies** — `BotResponse.ephemeral` exists on the contract,
  but the notification backend has no `chat.postEphemeral` (which needs a user id
  and is not threadable). v1 posts a threaded in-thread reply instead; real
  ephemeral lands if and when the backend grows the call.

### Limitation

The bot is **read-only and best-effort**. A mention is acked before it is handled
(Slack's ~3s Socket Mode window), so a lost reply is a re-ask, not a retry — the
deliberate trade for an interactive surface, the same one the interaction callbacks
make. No `@radar` command mutates state.

---

## Phase 10: Observability
**Milestone: v0.10-observability | Tag: v0.4.0**

Deliverables:
```
deploy/grafana/      all five dashboards as ConfigMaps
deploy/prometheus/   alerting rules (exists) + prometheus.yml + a compose mount
deploy/otel/         collector config
deploy/fluent-bit/   config
OTel trace coverage across all services confirmed
Fluent Bit log shipping confirmed
docs/operations/ runbooks for RADAR itself
plugins/traces/elastic/                              # deferred from Phase 8
```

All config lands in this repository under `deploy/`, not in a separate
radar-infra repo — see ADR 0018, which retired it.

**The dev stack's Prometheus is deliberately unwired until here.**
`deploy/prometheus/alerting-rules.yml` exists and is proven — two e2e tests mount
it against a real Prometheus and drive an alert through to ingestion — but
nothing in `deploy/compose/` mounts it, so the dev Prometheus runs on its default
config with no rules and no scrape targets.

Mounting the rules WITHOUT scrape configs would be worse than leaving it empty:
every alert would sit permanently inactive with no metrics behind it, looking
configured while being structurally unable to fire. Rules and scrape targets
land together, here, with alertmanager — which is what makes the
scrape -> fire -> webhook path real rather than declared.

Commits:
```
feat(observability): add otel collector daemonset config
feat(observability): add fluent bit daemonset config
feat(observability): add grafana dashboard provisioning
feat(observability): add prometheus alerting rules including llm fallback alert
docs(operations): add llm gateway failure runbook
docs(operations): add outbox backlog runbook
docs(operations): add vault secret rotation runbook
```

Done when: single mock alert traceable end to end in Kibana APM by correlation_id alone.
LLM fallback alert fires when gateway is mocked to fail.

---

## Phase 11: CI/CD
**Milestone: v0.11-cicd | Tag: v0.5.0**

Before touching workflow files: decide CD approach and write ADR 0012.
Options: self-hosted runner on a P400 (recommended, simpler) or Tailscale tunnel.

CI: path-based, builds only what changed. Multi-arch images. Tagged by git SHA.

Commits:
```
ci: add lint typecheck and test pipeline
ci: add changed service detection script
ci: add multi-arch docker buildx
ci: add helm validation
ci: add cd workflow to home lab
docs: add adr 0012 cd approach
docs: add cluster connectivity setup guide
```

Done when: changing feedback-service builds only feedback-service, and a change
under `deploy/` triggers no application build. Merge deploys it.

The second clause is not a nice-to-have. ADR 0018 retired the radar-infra
repository on the argument that path-based CI delivers the same release-cadence
isolation the split existed for — so `deploy/`-changes-nothing IS that decision's
justification. Until CI enforces it, the property is asserted and not proven, and
the single-repo decision ships unverified. Pin it with a test that fails if a
`deploy/`-only change queues an application build.

---

## Phase 12: Kubernetes and Helm
**Milestone: v0.12-kubernetes | Tag: v0.6.0**

Deliverables:
```
deploy/helm/radar/
deploy/examples/minimal/
deploy/examples/bring-your-own-backends/
```

Chart must have: resource limits, probes, Vault init-container, RBAC, HPA for
ingestion and llm-gateway, correlation rules and plan templates as ConfigMaps,
configurable backend providers.

Commits:
```
feat(helm): add radar application chart
feat(helm): add vault init-container per workload
feat(helm): add resource limits probes and rbac
feat(helm): add hpa for ingestion and llm-gateway
feat(helm): add correlation rules and plan templates as configmaps
feat(helm): add configurable backend providers
feat(deploy): add minimal and bring-your-own-backends examples
```

Done when: helm install deploys all services. All readiness probes pass.

---

## Phase 13: Security and Resilience Audit
**Milestone: v0.13-hardened | Tag: v0.7.0**

New work:
- Load test: 100 concurrent mock alerts, p50/p95/p99 from ingestion to recommendation
- Threat model document
- Circuit breaker in LLM Gateway
- Verify audit_log populated for all key events

Commits:
```
feat(llm-gateway): add circuit breaker for provider failures
feat(security): complete audit logging for all key events
test(load): add 100 concurrent alert load test
docs(security): add threat model
fix: address gaps from security audit
```

Done when: load test results documented. No data loss under load. Threat model written.

---

## Phase 14: Open Source Polish
**Milestone: v1.0 | Tag: v1.0.0**

Only after v0.7.0 is confirmed working and load-tested.

Deliverables:
```
Professional README with architecture diagram
15-minute quickstart (test on a clean machine)
Plugin development guide
Performance benchmark
SRE portfolio case study
Completed CHANGELOG and CONTRIBUTING
```

Commits:
```
docs: add professional readme with architecture diagram
docs: add 15 minute quickstart
docs: add plugin development guide
docs: add performance benchmark results
docs: add sre portfolio case study
docs: finalize changelog and contributing
```

Done when: someone else can run the local demo in 15 minutes from the README alone.

---

## Git State Per Phase

```
Phase 0    docs/ ADRs architecture only. Zero src code.
Phase 1  + Makefile pyproject.toml compose stack
Phase 2  + packages/contracts packages/plugin-sdk
Phase 3  + packages/common packages/database packages/telemetry
Phase 4  + apps/llm-gateway plugins/llm/
Phase 5  + apps/ingestion apps/platform-sim plugins/logs plugins/metrics
Phase 6  + apps/outbox-worker
Phase 7  + apps/watcher-agent apps/planner-agent apps/reasoner-agent
           tests/e2e/
           TAG: v0.1.0
Phase 8  + apps/knowledge-service plugins/traces docs/runbooks (real content)
           TAG: v0.2.0
Phase 9  + apps/feedback-service plugins/notifications
           TAG: v0.3.0
Phase 10 + deploy/grafana deploy/otel deploy/fluent-bit docs/operations
           TAG: v0.4.0
Phase 11 + .github/workflows scripts/detect-changed-services.py
           TAG: v0.5.0
Phase 12 + deploy/helm/radar
           TAG: v0.6.0
Phase 13 + tests/load docs/architecture/threat-model.md
           TAG: v0.7.0
Phase 14   Polished docs case study benchmark
           TAG: v1.0.0
```

---

## First Vertical Slice

The only thing that matters until Phase 7 is complete:

```
POST /alerts/mock (or a crafted /alerts/prometheus body via a platform-sim
         chaos endpoint — see tests/e2e/test_platform_sim_alert_path.py)
  |
ingestion
  normalize -> fingerprint -> dedup -> INSERT incident + outbox_event (one tx)
  |
outbox_events (status=pending)
  |
outbox-worker
  SELECT FOR UPDATE SKIP LOCKED -> mark processing -> POST /events
  |
watcher-agent
  check idempotency -> load YAML rules -> correlate -> INSERT incident state
  -> INSERT outbox_event(plan_requested) + processed_events + audit_log (one tx)
  |
outbox-worker
  |
planner-agent
  check idempotency -> load YAML templates -> match template
  -> INSERT plan + outbox_event(reasoning_requested) + processed_events + audit_log (one tx)
  |
outbox-worker
  |
reasoner-agent
  check idempotency -> build context bundle
  -> POST /v1/complete (mode=extended, X-Radar-Agent-Token)
    [if 503: generate template fallback RCA instead]
  -> INSERT recommendation + outbox_event(recommendation.created) + processed_events + audit_log (one tx)
  |
llm-gateway
  validate token -> enforce mode -> retry -> fallback provider -> call OpenAI SDK
  |
recommendation in Postgres (is_fallback=false if LLM worked, true if not)
  |
outbox-worker
  |
feedback-service
  format Slack card -> POST to Slack API -> card appears in your channel
  |
Prometheus metrics + Kibana APM traces + Elasticsearch logs
```

---

## Non-Goals for V1

```
Anomaly detection inside RADAR
Autonomous remediation
Agent memory across incidents
Fine-tuning pipeline
PagerDuty
ServiceNow or ticketing
LangChain / LangGraph / LiteLLM
Redis
Jaeger
JWT or OAuth (static tokens are fine)
Multi-tenant
Custom UI
Kafka / NATS
```

---

## Claude Code Prompt Template

Paste at the start of every Claude Code session before giving any task.

```
You are building RADAR, an AI-powered Incident Intelligence Platform.

HARD RULES - never violate these:
- Python 3.12, FastAPI, Pydantic v2, SQLAlchemy async, uv
- No LangChain, LangGraph, LiteLLM or any orchestration framework. Ever.
- No Redis. No Jaeger.
- LLM calls only from agents to llm-gateway via POST /v1/complete or /v1/embed
- Inter-agent communication only via Postgres outbox_events table
- No direct HTTP calls between agents
- Secrets only from Vault secret files. Never environment variables for secrets.
- structlog for all logging. JSON to stdout.
- Every service needs GET /healthz, GET /readyz, GET /metrics, POST /events
- X-Radar-Agent-Token header required on all non-health non-metrics endpoints
- All outbox writes must be in the same transaction as the triggering state change
- Every agent must check processed_events before handling any event
- All images must build for linux/amd64 AND linux/arm64 via docker buildx
- pytest-asyncio for all async tests
- mypy strict must pass
- Default LLM provider is OpenAI. All modes use OpenAI unless config says otherwise.

CURRENT PHASE: [paste phase name and milestone]

DELIVERABLES:
[paste exact deliverable list from the phase]

TASK:
[paste specific task]

Do not build outside the current phase deliverables.
If a design decision is unclear, ask before implementing.
Do not add dependencies not in the approved list.
```

---

## Summary

```
14 phases
1 repo     : radar-system
2 namespaces: radar, radar-infra
3 agents   : Watcher (correlate), Planner (plan), Reasoner (RCA + fallback)
1 gateway  : raw Python, 3 SDKs, 4 modes, token IAM, provider fallback
1 transport: Postgres transactional outbox
1 channel  : Slack (RCA cards + bot queries)
1 domain   : e-commerce order-service
1 provider : OpenAI (default, swap via config)
0 frameworks
0 Redis
0 Jaeger
```
-e

---

# ADR 0003: Postgres Transactional Outbox Over Kafka or NATS

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Context

RADAR agents need to communicate. When ingestion creates an incident, watcher needs
to know. When watcher correlates it, planner needs to know. This chain has to be
reliable. Dropped events mean incidents with no RCA. That is the worst outcome.

The obvious choices were a message broker (Kafka, NATS, RabbitMQ) or building
something on top of the database we already have (Postgres).

---

## Decision

Use the transactional outbox pattern on Postgres. No external message broker.

Every inter-agent event is written as a row in the `outbox_events` table inside the
same database transaction as the state change that triggered it. A dedicated
outbox-worker process polls this table and dispatches events via HTTP to target
services.

---

## Why Not Kafka

Kafka is the right choice when you need high throughput, multiple independent
consumers per topic, long-term event retention, or replay from any point in history.

RADAR has none of those requirements in v1. You have one pipeline with three agents
processing one event each in sequence. Peak load is tens of incidents per hour, not
millions of events per second.

What Kafka adds for this use case:

- Another stateful system to operate (brokers, ZooKeeper or KRaft, topic configs)
- Schema registry or manual schema versioning per topic
- Consumer group coordination complexity
- A completely separate failure domain to monitor and alert on
- Significant local dev overhead (Kafka is not trivial to run in docker compose)
- Steeper debugging curve: when something goes wrong, you are now debugging
  two systems instead of one

This is a solo project on a home lab with a MacBook as the control plane. Adding
Kafka would double the operational surface area before a single line of application
code runs.

---

## Why Not NATS

NATS is lighter than Kafka and fits smaller deployments better. But:

- It is still an external system to run, monitor, and understand
- NATS JetStream (needed for persistence) adds configuration complexity
- At-least-once delivery still requires consumer-side idempotency, which you need
  to build anyway
- Debugging a NATS consumer failure is harder than debugging a Postgres row

The argument for NATS is usually "it is simpler than Kafka." That is true. But
"simpler than Kafka" is not the same as "simpler than Postgres you already have."

---

## Why the Outbox Pattern Works Here

The critical property is atomicity. When ingestion creates an incident, the outbox
event for the watcher must either both commit or both roll back. There is no world
where the incident exists but the watcher never gets triggered, or vice versa.

With an external broker you lose this guarantee unless you implement a two-phase
commit or an outbox pattern anyway. So you end up building the outbox pattern on top
of Kafka, which is strictly worse than just using the outbox pattern on Postgres.

The outbox pattern on Postgres gives you:
- Atomicity between state change and event, guaranteed by the database
- Zero additional infrastructure to operate
- Dead letter handling as a simple status column
- Full event history queryable with SQL
- Replay by updating `status` back to `pending`
- Debugging by reading rows in a table, not decoding binary log formats
- Idempotency via the `processed_events` table and `event_id`

---

## Tradeoffs Accepted

This approach has real limitations that are acceptable for v1 but worth knowing:

**Single worker bottleneck**: the outbox-worker is a single process. If it crashes,
the pipeline stops until it restarts. Kubernetes restarts it automatically but there
is a gap. Kafka would give you consumer group redundancy. Accept this for v1.

**Polling overhead**: the worker polls every 2 seconds. This is 2 seconds of added
latency per hop in the pipeline. With three agents that is up to 6 seconds of
dispatch latency on top of LLM call time. Acceptable for an incident response
platform where end-to-end latency is measured in minutes, not milliseconds.

**Postgres under load**: if incident volume spikes significantly, the outbox table
becomes a hot write path. At homelab scale this is not a concern. At production
scale with thousands of incidents per hour, you would revisit this.

**No fan-out**: one event goes to one target service. If you ever need multiple
consumers for the same event you need to write multiple outbox rows. Kafka handles
fan-out natively. This is not a v1 requirement.

---

## Migration Path If You Outgrow This

If RADAR scales to the point where the outbox pattern is a bottleneck (thousands of
incidents per hour, multiple consumers needed), the migration path is:

1. Keep the outbox table as the write side
2. Add a Kafka producer to the outbox-worker that publishes events to topics
3. Migrate consumers from HTTP endpoints to Kafka consumers incrementally
4. Remove the HTTP dispatch path once all consumers are on Kafka

The application code barely changes. The outbox-worker changes. Everything else stays.

This is why the pattern is a good choice even beyond v1: it does not paint you into
a corner.

---

## Decision Record

Postgres transactional outbox for v1 and likely beyond. Revisit only if incident
volume exceeds what a single Postgres instance can handle comfortably, which at
homelab scale will not happen.
-e

---

# ADR 0004: No LangChain, LangGraph, or LiteLLM

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Context

Building an AI system that calls LLMs means you will encounter LangChain, LangGraph,
and LiteLLM within five minutes of googling. They are popular, widely used, and
constantly recommended. This ADR explains why RADAR uses none of them.

---

## Decision

RADAR calls LLM provider SDKs directly through a custom gateway. No orchestration
framework. No abstraction layer beyond what we write ourselves.

---

## Why Not LangChain

LangChain is an abstraction layer on top of LLM calls that also includes chains,
agents, memory, tools, retrievers, and a large ecosystem of integrations.

The problems:

**It abstracts the wrong things.** LangChain wraps the LLM call itself in layers
of indirection. When something breaks, you are debugging LangChain internals, not
your code. The stack traces are long, the error messages are vague, and the source
of truth is spread across multiple abstraction layers.

**The abstraction leaks constantly.** Every non-trivial use case requires dropping
down to provider-specific behavior anyway. At that point you are fighting the
abstraction rather than using it.

**It changes rapidly and breaks things.** LangChain has an aggressive release
cadence with frequent breaking changes. Pinning a version works until you need a
bug fix, then you upgrade and spend a week fixing breakage. For a platform that
needs to be stable and debuggable at 3am, this is a real risk.

**It adds a supply chain dependency.** LangChain pulls in dozens of transitive
dependencies. Each one is a potential vulnerability, a version conflict, or a
source of unexpected behavior. RADAR's LLM gateway has three dependencies: the
Anthropic SDK, the OpenAI SDK, and the Gemini SDK. That is it.

**It does not fit the architecture.** RADAR has a custom gateway with mode-based
IAM, per-mode timeouts, fallback providers, and audit logging. Implementing this
correctly inside LangChain is harder than implementing it without LangChain. The
framework would be fighting the design.

**It hides what you are actually doing.** For a portfolio project and a future
open-source tool, the code needs to be readable and understandable by someone who
has never seen it before. LangChain code is not readable to someone unfamiliar with
its abstractions. Raw Python calling an SDK is.

---

## Why Not LangGraph

LangGraph is LangChain's answer to agent orchestration. It models agent pipelines
as graphs with nodes and edges.

The additional problems:

**RADAR's pipeline is linear.** Watcher -> Planner -> Reasoner. It is not a graph.
Using a graph framework to model a linear pipeline is using a sledgehammer on a nail.

**The Postgres outbox is already the orchestration layer.** Events flow between
agents via the outbox. The outbox is the graph. Adding LangGraph on top means two
orchestration systems that need to stay in sync.

**Debugging graph-based agents is harder than debugging sequential code.** When
Planner fails, you want to look at a log line and understand exactly what happened.
With LangGraph you are looking at graph traversal state and trying to reconstruct
what the framework decided to do.

---

## Why Not LiteLLM

LiteLLM is a proxy/abstraction layer that provides a unified API across LLM
providers. The argument for it is that you write one integration and get all
providers for free.

The problems:

**RADAR already has this.** The LLM gateway provides a unified internal API.
Providers are plugins. Swapping providers is a config change. LiteLLM solves a
problem that is already solved, just differently.

**It is another service or library to depend on.** LiteLLM as a proxy means another
deployment to manage. LiteLLM as a library means another set of transitive
dependencies and another thing that can break.

**Supply chain risk.** LiteLLM has had documented security issues in the past.
For a system that handles operational alerts and calls external APIs with real
credentials, supply chain risk is not theoretical. Fewer dependencies means a
smaller attack surface.

**It abstracts provider-specific behavior.** Each provider has quirks: different
token counting methods, different streaming formats, different error codes, different
retry behavior. LiteLLM normalizes these, which sounds good until you need to debug
why your Gemini calls are failing differently from your OpenAI calls. The abstraction
hides information you need.

**Loss of control over the gateway.** RADAR's gateway enforces mode-based IAM,
per-mode token limits, specific timeouts, and audit logging. Doing this cleanly
through LiteLLM is harder than doing it directly. You end up wrapping LiteLLM in
your own layer, which defeats the point.

---

## What We Do Instead

The LLM gateway is raw Python:

- Each provider has one file: `anthropic_provider.py`, `openai_provider.py`,
  `gemini_provider.py`
- Each implements the `LLMProvider` protocol from `packages/contracts`
- The gateway routes requests to the right provider based on mode config
- Retry, timeout, fallback, and audit logging are implemented once in the gateway
- The whole thing is under 500 lines of code and readable by anyone who knows Python

When something breaks, you read the code. There is no framework to understand first.

---

## Tradeoffs Accepted

**More code to write upfront.** Writing provider adapters from scratch takes longer
than installing LangChain. This is a one-time cost that pays off every time you
debug something in production.

**No ecosystem integrations.** LangChain has integrations with hundreds of tools.
RADAR does not need them. If it ever does, writing a specific integration is less
risky than pulling in the entire ecosystem.

**Manual updates when provider SDKs change.** When Anthropic releases a breaking
change, you update `anthropic_provider.py`. With LangChain you wait for LangChain
to update their wrapper and then update your LangChain version. The manual path
is faster and more predictable.

---

## Decision Record

No LangChain. No LangGraph. No LiteLLM. Raw Python with direct SDK calls through
a custom gateway. This decision does not get revisited unless RADAR needs to support
20+ providers simultaneously, which is not a v1, v2, or likely v3 requirement.
-e

---

# ADR 0013: Static Token Auth for Internal Services in V1

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Context

RADAR services call each other. The outbox-worker calls agent HTTP endpoints. Agents
call the LLM gateway. The knowledge service calls the LLM gateway. These internal
calls need some form of authentication so that a misconfigured or compromised service
cannot call the LLM gateway and run up an API bill or exfiltrate context.

The options considered were: no auth, static shared tokens, JWT with short TTL,
and mutual TLS.

---

## Decision

Static 32-byte hex tokens per service. One token per service. Stored in Vault.
Loaded at startup via init-container. Validated on every internal request.

Token format: `secrets.token_hex(32)` which produces a 64-character hex string.

At the LLM gateway specifically, each token also maps to exactly one allowed mode,
enforcing that watcher can only make fast calls, reasoner can only make extended
calls, and so on.

---

## Why Not No Auth

No internal auth means any process that can reach the LLM gateway can call it.
In a Kubernetes cluster with network policies this is partially mitigated but:

- Network policies are another thing to get right and maintain
- A bug in any service that makes it call the gateway unintentionally would go
  through unchecked
- The mode restriction (watcher cannot make extended calls) would not be enforceable
- There is no audit trail of which service made which LLM call

Static tokens add two lines of code per service and one config value. The cost is
negligible and the benefit is real.

---

## Why Not JWT

JWT with short TTL is the common recommendation for internal service auth in
microservices. The argument is that short-lived tokens limit the blast radius of
a compromised token.

The problems for this specific use case:

**Agents are Kubernetes deployments, not humans.** JWT short TTL makes sense when
a user logs in, gets a token, and uses it for an hour. It does not make much sense
when a pod is running 24/7 and needs a valid token at all times. Short TTL means
you need a token refresh mechanism, which means:
- A token issuing service or endpoint
- Logic in every service to detect expiry and refresh
- Handling the race condition where a token expires mid-request
- Something to go wrong at 3am when the token issuer has an outage

**The security benefit is smaller than it appears.** A compromised container in
your Kubernetes cluster can read memory, environment variables, and mounted files.
If an attacker has that level of access, the difference between a 15-minute JWT
and a long-lived token is not the critical security boundary. The critical boundary
is preventing the container from being compromised in the first place.

**It adds complexity before value is proven.** RADAR does not have users. It does
not have external-facing auth. Adding JWT infrastructure now is solving a problem
that does not exist yet.

---

## Why Not Mutual TLS

mTLS means every service has a certificate and every service validates the caller's
certificate. This is the gold standard for internal service auth in production
microservices at companies with dedicated platform teams.

For this project:

**Certificate management is a significant operational burden.** You need a CA,
certificate issuance, rotation, and distribution. Tools like cert-manager help but
add another moving part. On a home lab with a MacBook as the control plane, this
is substantial overhead.

**It provides authenticity, not authorization.** mTLS tells you that the caller
is who they say they are, but it does not tell you what they are allowed to do.
For the mode restriction (watcher cannot call extended) you still need
application-level enforcement on top of mTLS. So you end up with mTLS plus tokens
anyway.

**It is a production-grade solution for a system that has not proven its design
yet.** Invest in mTLS after the system works and has users. Not before.

---

## What This Looks Like in Practice

Vault stores one secret per service:

```
secret/radar/watcher-agent    -> agent_token: <64 char hex>
secret/radar/planner-agent    -> agent_token: <64 char hex>
secret/radar/reasoner-agent   -> agent_token: <64 char hex>
secret/radar/knowledge-service -> agent_token: <64 char hex>
secret/radar/outbox-worker    -> agent_token: <64 char hex>
```

The init-container writes the token to `/vault/secrets/agent_token` at pod startup.
The service reads it once at startup via the config loader. If the file is missing,
`/readyz` returns 503 and the pod does not receive traffic.

Every internal HTTP call includes the header `X-Radar-Agent-Token: <token>`.
Services validate this in a FastAPI dependency that runs before every non-health
handler.

If a token is compromised:
1. Generate a new token: `python -c "import secrets; print(secrets.token_hex(32))"`
2. Update the Vault secret
3. Restart the affected pod

Total recovery time: under 2 minutes. No token issuer to fix, no certificate to
rotate, no distributed secret to invalidate.

---

## Security Properties This Provides

- Internal endpoints are not callable without a valid token
- Each service has a unique token, so a compromised token is scoped to one service
- The LLM gateway enforces mode restrictions per token, limiting blast radius
- Tokens are never in logs, never in environment variables, never in code
- Vault access is controlled by Kubernetes service account roles
- The audit log records which service made which LLM call

---

## Security Properties This Does Not Provide

- Protection against a compromised container reading its own token from memory
- Cryptographic proof of caller identity (that is mTLS)
- Short-lived credentials that auto-expire
- Fine-grained per-request authorization

These are acceptable gaps for v1 on a homelab.

---

## Migration Path

When RADAR has external users, real production traffic, and a dedicated ops concern
for security:

1. Keep static tokens as the fallback
2. Add cert-manager to the cluster
3. Issue certificates per service via cert-manager
4. Enable mTLS via a service mesh (Linkerd is lighter than Istio)
5. Remove token validation once mTLS is fully rolled out

The migration is incremental and does not require rewriting application code.

---

## Decision Record

Static 32-byte hex tokens in Vault for v1. Revisit for v2 if RADAR has external
users or a security audit that identifies this as an unacceptable risk.
-e

---

# ADR 0014: Event Schema Versioning Rules

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Context

Outbox events carry a JSON payload. As RADAR evolves, these payloads will change.
A field gets added. A field gets renamed. A field gets removed. Without rules for
how this happens, you end up with a pile of events in the outbox that a newer
version of an agent cannot parse, or worse, silently misreads.

This document defines the rules.

---

## Event Structure

Every outbox event has this envelope, which never changes:

```json
{
  "event_id": "uuid",
  "event_type": "alert.normalized",
  "schema_version": "1",
  "correlation_id": "uuid",
  "occurred_at": "2025-01-15T10:30:00Z",
  "payload": {}
}
```

The `payload` field is where schema changes happen. The envelope fields are frozen.

---

## Versioning Scheme

`schema_version` is a simple integer starting at 1. It increments by 1 for every
breaking change. Non-breaking changes do not increment the version.

```
schema_version: "1"    initial payload shape
schema_version: "2"    first breaking change
schema_version: "3"    second breaking change
```

No semver. No minor versions. No patch versions. One number. Breaking or not breaking.

---

## What Is a Breaking Change

A breaking change is anything that requires the consumer to update its parsing code
to avoid a runtime error or silent data corruption.

Breaking changes:
- Removing a field the consumer reads
- Renaming a field the consumer reads
- Changing the type of a field (string to integer, object to array)
- Changing the semantic meaning of a field value (status codes, enum values)

Not breaking changes:
- Adding a new optional field the consumer does not need to read
- Adding a new enum value the consumer handles with a default case
- Adding fields to a nested object the consumer ignores

---

## Rules

### Rule 1: Never remove or rename a field without incrementing the version

If you remove `service_name` from `alert.normalized`, existing events in the outbox
with the old schema will fail to parse. Consumers that already processed some events
will break on the next batch.

Add a new field instead of renaming. Deprecate the old one. Remove it only after
all consumers have migrated and no old-schema events remain in the outbox.

### Rule 2: Consumers must handle unknown fields without crashing

Use Pydantic's `model_config = ConfigDict(extra="ignore")` on all event payload
models. A consumer receiving an event with extra fields it does not recognize must
ignore them and continue processing.

This makes adding new fields non-breaking by definition.

```python
class AlertNormalizedPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")

    event_id: UUID
    service_name: str
    alert_name: str
    severity: str
    fired_at: datetime
```

### Rule 3: Consumers must check schema_version before parsing

Each consumer reads the `schema_version` from the envelope before parsing the
payload. If the version is higher than the consumer knows how to handle, the
consumer rejects the event and writes it to the dead letter queue with reason
`schema_version_unsupported`.

```python
SUPPORTED_SCHEMA_VERSIONS = {"alert.normalized": {"1", "2"}}

def can_handle(event: OutboxEvent) -> bool:
    supported = SUPPORTED_SCHEMA_VERSIONS.get(event.event_type, set())
    return event.schema_version in supported
```

This prevents silent data corruption from a consumer misreading a newer event shape.

### Rule 4: Both schema versions must be supported during any transition

When you introduce schema_version 2 for `alert.normalized`, the consumer must
handle both version 1 and version 2 simultaneously for a transition period. This
period lasts until all version 1 events have been processed and drained from the
outbox.

```python
def parse_alert_normalized(event: OutboxEvent) -> AlertNormalizedPayload:
    if event.schema_version == "1":
        return AlertNormalizedPayloadV1.model_validate(event.payload)
    if event.schema_version == "2":
        return AlertNormalizedPayloadV2.model_validate(event.payload)
    raise UnsupportedSchemaVersion(event.schema_version)
```

### Rule 5: Old schema versions are removed only when the outbox is fully drained

Do not remove support for schema_version 1 while there are still version 1 events
sitting in the outbox (status=pending or status=dead_letter). Check the outbox table
before removing old version handling:

```sql
SELECT COUNT(*) FROM outbox_events
WHERE event_type = 'alert.normalized'
  AND payload->>'schema_version' = '1'
  AND status IN ('pending', 'processing', 'dead_letter');
```

If the count is zero, it is safe to remove v1 handling.

### Rule 6: Schema changes are documented in CHANGELOG.md

Every schema version bump gets a CHANGELOG entry:

```markdown
## [Unreleased]
### Changed
- alert.normalized: schema_version 1 -> 2
  - Added: `alert_group_id` (UUID, optional) for multi-alert correlation
  - Deprecated: none
  - Removed: none
  - Migration: consumers must handle both v1 (no alert_group_id) and v2
```

---

## Current Event Types and Versions

```
alert.normalized              schema_version: 1
incident.plan_requested       schema_version: 1
incident.reasoning_requested  schema_version: 1
recommendation.created        schema_version: 1
```

All at version 1. This is the baseline. Any change to any of these payloads must
follow the rules above.

---

## Payload Schemas (v1)

### alert.normalized

```json
{
  "alert_id": "uuid",
  "source": "prometheus",
  "fingerprint": "sha256hex",
  "service_name": "order-service",
  "alert_name": "OrderProcessingFailureRate",
  "severity": "critical",
  "labels": {},
  "annotations": {},
  "fired_at": "2025-01-15T10:30:00Z"
}
```

### incident.plan_requested

```json
{
  "incident_id": "uuid",
  "service_name": "order-service",
  "alert_name": "OrderProcessingFailureRate",
  "severity": "critical",
  "alert_count": 1,
  "opened_at": "2025-01-15T10:30:00Z"
}
```

### incident.reasoning_requested

```json
{
  "incident_id": "uuid",
  "plan_id": "uuid",
  "service_name": "order-service",
  "alert_name": "OrderProcessingFailureRate",
  "severity": "critical",
  "alert_count": 1,
  "opened_at": "2025-01-15T10:30:00Z"
}
```

### recommendation.created

```json
{
  "recommendation_id": "uuid",
  "incident_id": "uuid",
  "is_fallback": false,
  "confidence": "medium",
  "service_name": "order-service"
}
```

---

## Decision Record

Integer schema versions. Extra fields ignored. Version checked before parsing.
Both versions supported during transitions. Old versions removed only after drain.
All changes documented in CHANGELOG.
-e

---

# ADR 0015: Database Migration Rules

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Context

RADAR uses Postgres. The schema will change as the product evolves. Without strict
rules around how migrations are written and run, you end up with migrations that
corrupt data, break running services, or cannot be rolled back.

These rules apply to every migration, no exceptions.

---

## Tooling

Alembic manages migrations. Migration files live in `packages/database/migrations/versions/`.
Every migration is a Python file generated by Alembic with a unique revision ID.

---

## Rule 1: Every migration must be reversible

Every migration file must implement both `upgrade()` and `downgrade()`. If you
cannot write a `downgrade()`, you do not merge the migration.

The only exception is irreversible data transformations (e.g. hashing plain text
values). In that case the migration must have a comment explicitly stating why
downgrade is not possible and what the manual recovery path is.

```python
def upgrade() -> None:
    op.add_column("incidents", sa.Column("severity_score", sa.Integer()))

def downgrade() -> None:
    op.drop_column("incidents", "severity_score")
```

### Rule 2: Migrations must be backward compatible with the running service version

The database schema after a migration must work with the previous version of the
application code. This is because in a rolling deployment, new pods come up before
old pods come down. During the transition, both the old code and the new code are
running against the same database.

Concretely:
- Adding a nullable column is backward compatible. Old code ignores it.
- Adding a NOT NULL column without a default is not backward compatible. Old code
  cannot insert rows because it does not know to supply the new column.
- Renaming a column is not backward compatible. Old code reads the old name.
- Dropping a column is not backward compatible. Old code reads a column that does
  not exist anymore.

The deploy sequence is always: run migration first, then deploy new application code.
This means the new schema must work with old code.

---

### Rule 3: Non-null columns require a two-step migration

If you need a NOT NULL column without a default:

Step 1 (migration A): Add the column as nullable.
```python
op.add_column("incidents", sa.Column("region", sa.String(64), nullable=True))
```

Deploy new application code that writes the column on every new row.

Step 2 (migration B, separate PR): Backfill existing rows, then add the NOT NULL constraint.
```python
op.execute("UPDATE incidents SET region = 'unknown' WHERE region IS NULL")
op.alter_column("incidents", "region", nullable=False)
```

Never combine these into one migration. The gap between them is intentional.

---

### Rule 4: Never use Alembic autogenerate blindly

`alembic revision --autogenerate` is useful for detecting what changed but its
output must be reviewed and cleaned up before committing. Autogenerate frequently:

- Generates unnecessary index drops and recreations
- Misses partial indexes
- Generates table drops when you only wanted a column drop
- Gets confused by custom types

Always read the generated migration before committing it. If it looks wrong, it is.

---

### Rule 5: Migrations run once and never get edited after merge

Once a migration is merged to main, it is immutable. If you made a mistake in a
migration, write a new migration that corrects it. Never edit the existing file.

Editing a migration after it has run on any environment breaks the Alembic revision
chain. You will end up with databases that have run different versions of the same
revision ID. That is not a debugging problem you want.

---

### Rule 6: Migration filenames must be descriptive

Alembic generates revision IDs like `ae1027a6acf`. The filename also needs a
description:

Good: `ae1027a6acf_add_severity_score_to_incidents.py`
Bad: `ae1027a6acf_.py`

Generate with:
```
alembic revision --autogenerate -m "add_severity_score_to_incidents"
```

---

### Rule 7: Every migration is tested in CI against a real Postgres instance

The CI pipeline runs `alembic upgrade head` and then `alembic downgrade -1` on a
fresh Postgres container. If either direction fails, the PR does not merge.

This catches:

- Syntax errors in migration files
- Migrations that reference columns or tables that do not exist
- Downgrade paths that were written incorrectly
- Constraint violations during data migrations

---

### Rule 8: Production migrations run before code deployment

The deploy order is always:

1. Run `alembic upgrade head` against the production database
2. Verify the migration succeeded (check `alembic_version` table)
3. Deploy new application code
4. If deployment fails: roll back application code, run `alembic downgrade -1`

Never deploy new code first and run the migration after. See Rule 2 for why.

---

### Rule 9: Long-running migrations must be done with care

Adding an index on a large table takes a lock and can block writes for minutes.
In Postgres you can create indexes concurrently to avoid this:

```python
# In the migration
op.create_index(
    "idx_incidents_opened_at",
    "incidents",
    ["opened_at"],
    postgresql_concurrently=True
)
```

Any migration that touches more than 100k rows or adds an index to a table with
active writes must use concurrent operations or be done during a maintenance window.

At homelab scale this is not a concern yet. The rule exists so you do not forget
it when RADAR has real traffic.

---

### Rule 10: The audit_log table never gets a destructive migration

`audit_log` is append-only by design. Migrations on this table may only:
- Add new columns (nullable)
- Add indexes

Never drop columns, drop the table, truncate rows, or add constraints that could
reject existing rows. The audit log is the paper trail. Destroying it destroys
the audit.

---

## Migration Checklist

Before opening a PR with a migration, verify:

```
[ ] upgrade() and downgrade() both implemented
[ ] downgrade() actually reverses upgrade() (tested locally)
[ ] New NOT NULL columns use the two-step pattern
[ ] Migration tested with alembic upgrade head && alembic downgrade -1
[ ] Filename is descriptive
[ ] No autogenerate artifacts left in (unnecessary drops, duplicate indexes)
[ ] CHANGELOG.md updated with schema change description
[ ] If adding an index: used postgresql_concurrently=True if table has data
[ ] If touching audit_log: only additive changes
```

---

## Decision Record

Alembic for migrations. All rules above apply to every migration with no exceptions.
Reversibility and backward compatibility are non-negotiable. The checklist runs
in CI and in code review.
-e

---

# ADR 0016: Incident Lifecycle State Machine

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Context

An incident in RADAR goes through several states from the moment the first alert
fires to the moment it is closed. Without a defined state machine, services write
whatever status they feel like, transitions happen inconsistently, and the Slack bot
returns confusing status information to engineers.

This document defines the state machine. Every service that touches an incident must
follow it.

---

## States

```
open
investigating
resolved
closed
```

That is it. Four states. Not six. Not ten.

---

## State Definitions

### open

The incident has been created. At least one alert has fired. The pipeline has not
yet produced a recommendation. This is the initial state.

An incident stays in `open` from the moment it is created until the reasoner agent
produces a recommendation (or a fallback recommendation). If no recommendation is
produced within 10 minutes, the `IncidentRCAStalled` Prometheus alert fires.

### investigating

The reasoner agent has written a recommendation. The Slack card has been sent.
The incident is now in the hands of an on-call engineer.

Transition to `investigating` happens automatically when `recommendation.created`
outbox event is processed by feedback-service.

An incident can stay in `investigating` indefinitely. It transitions out when an
engineer takes action: either marking it resolved via Slack feedback or the source
alerts resolve.

### resolved

The incident has been addressed. Either:
- An engineer clicked "resolved" in the Slack card or bot command
- The original alert resolved in Prometheus and ingestion received a resolved payload

When an incident resolves, `resolved_at` is set. The incident may still have
open follow-up questions but the immediate operational impact is over.

### closed

The incident has been reviewed, documented, and closed. In v1 this is a manual
action via the Slack bot (`@radar close INC-abc123`). In a future version this
could trigger a post-incident review workflow.

`closed_at` is set when transitioning to this state. Closed incidents do not appear
in `@radar open` results.

> **Amendment 3 (Phase 9): `closed_at` is RESERVED, not yet reached.** The
> `resolved -> closed` edge and its `closed_at` stamp exist in the shipped state
> machine (`transition_status` sets `closed_at` on that transition), but no Phase 9
> caller performs it: `@radar close` is deferred, so in practice `closed_at` stays
> NULL until that command lands. Recorded here so the column reads as deliberately
> reserved rather than silently orphaned. See Amendments (Phase 9) below.

---

## State Transition Diagram

```
                    alert fires
                        |
                        v
                     [ open ]
                        |
                        | reasoner writes recommendation
                        v
                  [ investigating ]
                        |
              __________|__________
             |                     |
             | engineer resolves   | alert resolves in Prometheus
             v                     v
           [ resolved ] <---------+
                |
                | engineer closes (manual, @radar close)
                v
            [ closed ]
```

---

## Valid Transitions

```
open          -> investigating    (recommendation written by reasoner)
open          -> resolved         (alert resolved before recommendation written)
investigating -> resolved         (engineer marks resolved, or alert resolves)
investigating -> open             NOT ALLOWED (regression)
resolved      -> closed           (engineer closes)
resolved      -> investigating    NOT ALLOWED (incident is resolved, open a new one)
closed        -> any              NOT ALLOWED (closed is terminal)
```

If a service attempts an invalid transition, it must log an error, write to
`audit_log`, and reject the state change. It must not silently accept an invalid
transition.

---

## Who Can Trigger Each Transition

```
open -> investigating          : feedback-service (on recommendation.created — see Amendment 1)
open -> resolved               : ingestion (alert resolved payload received)
investigating -> resolved      : ingestion (last firing alert resolves) OR
                                 feedback-service (engineer Slack action) — see Amendment 2
resolved -> closed             : feedback-service (Slack bot command @radar close)
```

No other service changes incident status. This is enforced by the repository layer.
See **Amendments (Phase 9)** below for the two authority corrections above.

The `IncidentRepository.transition_status()` method validates the transition before
writing:

```python
VALID_TRANSITIONS = {
    "open": {"investigating", "resolved"},
    "investigating": {"resolved"},
    "resolved": {"closed"},
    "closed": set(),
}

async def transition_status(
    self,
    incident_id: UUID,
    new_status: str,
    actor: str,
    session: AsyncSession
) -> Incident:
    incident = await self.get(incident_id, session)
    valid_next = VALID_TRANSITIONS.get(incident.status, set())

    if new_status not in valid_next:
        raise InvalidStateTransitionError(
            f"Cannot transition {incident.status} -> {new_status} "
            f"for incident {incident_id}"
        )

    incident.status = new_status
    incident.updated_at = datetime.utcnow()

    if new_status == "resolved":
        incident.resolved_at = datetime.utcnow()
    if new_status == "closed":
        incident.closed_at = datetime.utcnow()

    await session.flush()
    return incident
```

---

## Audit Log Entries Per Transition

Every status transition writes to `audit_log`. No exceptions.

```
open -> investigating:
  event_type: incident.investigating
  actor: reasoner-agent
  payload: {recommendation_id, is_fallback, confidence}

open -> resolved (alert resolved):
  event_type: incident.resolved
  actor: ingestion
  payload: {resolved_by: "alert_resolution", alert_source}

investigating -> resolved (engineer):
  event_type: incident.resolved
  actor: slack_user_id
  payload: {resolved_by: "engineer", slack_user_id}

resolved -> closed:
  event_type: incident.closed
  actor: slack_user_id
  payload: {slack_user_id}

invalid transition attempt:
  event_type: incident.invalid_transition
  actor: <service that attempted it>
  payload: {from_status, attempted_status, reason: "invalid_transition"}
```

---

## Stale Incident Handling

An incident that stays in `open` for more than 10 minutes without a recommendation
is considered stalled. This fires the `IncidentRCAStalled` Prometheus alert.

An incident that stays in `investigating` for more than 4 hours without a resolution
is considered stale. No automated action in v1. The Slack bot returns a warning
when queried:

```
@radar incident INC-abc123
-> [WARNING] This incident has been in investigating state for 6 hours.
   Last activity: recommendation written 6 hours ago.
```

In v2 a scheduled job could nag the on-call channel about stale incidents.

---

## Multiple Alerts on One Incident

When a second alert with the same fingerprint arrives while an incident is `open`
or `investigating`, it attaches to the existing incident. The incident `alert_count`
increments. No state transition happens.

When all attached alerts resolve in Prometheus and ingestion receives resolved
payloads for all of them, ingestion transitions the incident to `resolved`
automatically.

Partial resolution (some alerts resolved, some still firing) does not change
incident status. The incident stays in its current state.

---

## Amendments (Phase 9 — v0.9-feedback)

Recorded when `IncidentRepository.transition_status` was codified in
`packages/database`. The state machine itself (four states, four edges) is
unchanged; these correct the surrounding prose where it contradicted the shipped
enforcement, and are kept here rather than silently editing the original text.

**Amendment 1 — feedback-service owns `open -> investigating`, not reasoner-agent.**
The "Who Can Trigger" table originally attributed this transition to reasoner-agent;
the State Definitions section (unchanged) already said it happens "when
`recommendation.created` is processed by feedback-service," so the ADR contradicted
itself. feedback-service is correct, on two grounds that agree. *Structural:* the
service that PROCESSES an event performs the write — `recommendation.created` is
emitted by the reasoner and consumed by feedback-service, and emitting an event does
not make you the actor for a transition triggered by consuming it. *Semantic:*
`investigating` must mean "a human has been told," which is true when the card is
DELIVERED, not when the recommendation row is written — if the reasoner transitioned,
an incident would sit in `investigating` while Slack delivery was still queued in the
outbox, or had dead-lettered.

**Amendment 2 — ingestion's authority is `{open, investigating} -> resolved`.**
Originally ingestion was granted only `open -> resolved`. But an Alertmanager
`resolved` webhook usually arrives AFTER the RCA card has been sent, i.e. while the
incident is already `investigating` — the common case. Restricting ingestion to
`open -> resolved` left that common path unauthorized, describing a system that could
not handle its own primary flow. Ingestion may now resolve from either `open` or
`investigating`, actor `ingestion`, `resolved_by: "alert_resolution"`. The
alternative — routing the webhook through an outbox event to feedback-service — was
rejected because it makes incident resolution depend on a service that need not exist
yet, inverting the build order (ingestion-side lifecycle is proven BEFORE Slack).
This is consistent with "Multiple Alerts on One Incident" above, which already has
ingestion resolving the incident when its last firing alert resolves.

**Amendment 3 — `closed_at` is reserved, not orphaned.** See the note under the
`closed` state definition. The edge and stamp ship; no Phase 9 caller reaches them.

**Amendment 4 — the exception is named `InvalidStateTransitionError`.** The illustrative
code above originally wrote `InvalidStateTransition`; the shipped class carries the
`Error` suffix to match RADAR's error hierarchy (`ConflictError`, `NotFoundError`, …),
which the repo's ruff config (N818) enforces. It subclasses `ConflictError` (409): an
illegal transition is a conflict with current state, most often because the state moved
under the caller. The shipped `transition_status` signature also differs from the
sketch above — it takes the incident id (loaded under `SELECT ... FOR UPDATE`, not via
`get`), keyword-only `actor` / `correlation_id` / `audit_payload` / `occurred_at`,
writes the `audit_log` row for a valid transition in the caller's transaction, and
writes nothing on rejection (the caller records the `incident.invalid_transition`
attempt in its own transaction).

---

## Decision Record

Four states. Defined valid transitions. Transition validation in the repository
layer, not in services. Every transition writes to audit_log. Invalid transitions
are logged and rejected, not silently accepted.

---

# ADR 0017: Dead Letter Replay Strategy

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Context

The outbox worker retries failed event dispatches up to five times with exponential
backoff. After five failures, an event moves to `dead_letter` status and stops being
retried. At this point the incident pipeline is stalled for that event. An on-call
engineer or operator needs to decide what to do.

This document defines what happens to dead-letter events, how to investigate them,
how to replay them, and when to discard them instead.

---

## What Causes Dead Letters

An event reaches dead_letter status because the target service rejected or failed
to process it five consecutive times. Common causes:

**Target service is down.** The pod crashed and Kubernetes has not restarted it yet.
Or the pod is in a crash loop. The event fails because the HTTP call cannot connect.
Resolution: fix the service, then replay the event.

**Target service returned a 5xx.** The service is up but something inside it is
failing. A database connection problem, a dependency timeout, a bug. The outbox
worker retries on 5xx but eventually gives up. Resolution: fix the root cause, then
replay.

**Target service returned a 4xx.** This is different. A 422 means the payload was
invalid. A 401 means the token was wrong. These are not transient failures. Retrying
the same invalid payload will always fail. Resolution: investigate the payload,
fix the schema or the service, then decide whether to replay or discard.

**Schema version mismatch.** The target service does not know how to parse the
event's schema_version. This happens when a producer was updated but the consumer
was not. Resolution: update the consumer, then replay.

**The target service processed the event but crashed before acknowledging it.**
In this case the event retries and the service sees it again. This is why idempotency
exists. The service checks `processed_events` and returns 200 without doing the
work again. The outbox worker marks the event delivered. No dead letter.

---

## Dead Letter Detection

Prometheus alert:

```yaml
- alert: OutboxDeadLetterHigh
  expr: increase(radar_outbox_dead_letter_total[10m]) > 5
  annotations:
    summary: "Dead letter queue growing. Check outbox worker logs."
```

This fires if more than 5 events dead-letter in a 10-minute window. A single
dead-letter event does not fire the alert. A sustained rate does.

> **Corrected (Phase 9):** the paragraph originally here described `@radar status`
> reporting a dead-letter count and an `@radar dead-letters` command listing them.
> Neither shipped. `@radar status` reports open incidents, last RCA, and outbox
> depth only (`bot.py`'s `_run_status`) — no dead-letter count. There is no
> `@radar dead-letters` verb in `BotCommandType`. The real v1 way to inspect and
> replay dead letters is the admin HTTP endpoints (`curl`) under
> [Replay Mechanism](#replay-mechanism) below. A Slack-native dead-letter view is
> unbuilt, same status as `@radar replay` further down that section.

---

## Investigation Before Replay

Do not replay blindly. Before replaying any dead-letter event:

**Step 1: Read the last_error.**
The `outbox_events.last_error` column stores the last error message from the target
service. This tells you whether the failure was transient (connection refused, 503)
or structural (422, schema mismatch).

**Step 2: Check the target service.**
Is it running? `kubectl get pods -n radar`. Are there errors in its logs?
`kubectl logs -l app=<service> -n radar --since=1h | grep ERROR`.

**Step 3: Check the payload.**
Read the event payload from the `outbox_events` table. Does it look valid? Does it
match the current schema for that event_type?

```sql
SELECT event_id, event_type, payload, last_error, attempts
FROM outbox_events
WHERE status = 'dead_letter'
ORDER BY created_at DESC;
```

**Step 4: Check if the downstream work was already done.**
Before replaying `incident.plan_requested`, check whether a plan already exists
for the incident. Before replaying `recommendation.created`, check whether the
feedback-service already sent the Slack message. Replaying an already-completed
event is safe because of idempotency, but it is worth knowing.

---

## Replay Mechanism

The outbox worker exposes two admin endpoints:

```
GET  /admin/dead-letter
     Returns all dead-letter events with id, event_type, target_service,
     last_error, attempts, created_at, payload.

POST /admin/dead-letter/{event_id}/requeue
     Moves the event back to pending status.
     Resets attempts to 0.
     Sets process_after to NOW().
     The outbox worker picks it up on the next poll cycle.
```

Replay via the Slack bot (v1 is manual via kubectl port-forward or internal tooling):

```bash
# Port-forward to outbox-worker
kubectl port-forward svc/outbox-worker 8080:8080 -n radar

# List dead letters
curl http://localhost:8080/admin/dead-letter

# Replay a specific event
curl -X POST http://localhost:8080/admin/dead-letter/<event_id>/requeue
```

In v2 the Slack bot can expose `@radar replay <event_id>` as a command.

---

## Replay Rules

**Rule 1: Fix the root cause before replaying.**
Replaying an event into a broken service will just dead-letter it again. Fix the
service first. Then replay.

**Rule 2: Replay individual events, not batches.**
Do not requeue all dead-letter events at once. Replay one, verify it processes
successfully, then replay the next. A batch replay into a still-broken service
creates a second wave of dead letters.

**Rule 3: 4xx failures usually mean discard, not replay.**
If an event died with a 422, the payload is invalid. Replaying it will fail again
with the same 422. Either the service has a bug that needs fixing first, or the
event should be discarded. Investigate before deciding.

**Rule 4: Replayed events still go through idempotency checks.**
The target service checks `processed_events` for the event_id. If the event was
partially processed before it died, the service will detect that and return 200
without re-doing the work. This is correct behavior. Do not remove idempotency
checks to make replays easier.

**Rule 5: Write to audit_log when replaying.**
Every manual requeue must write an `outbox.event_requeued` entry to audit_log
with: event_id, event_type, operator (who did it), reason (why).

---

## When to Discard Instead of Replay

Discard a dead-letter event when:

**The incident is already resolved.** If `incident.plan_requested` dead-lettered
but the incident is already `resolved` or `closed`, there is no point generating
a plan. Discard the event and close the dead-letter entry with reason `stale`.

**The payload is invalid and cannot be fixed.** If the event carries a malformed
payload that no version of the consumer can parse, and there is no way to reconstruct
the correct payload, discard it. Log the discard to audit_log with reason
`invalid_payload_unrecoverable`.

**The event is older than 24 hours.** A `recommendation.created` event that has
been sitting in dead-letter for 24 hours means the Slack notification is 24 hours
late. Sending it now is worse than not sending it. Discard it, mark the incident
as needing manual review.

Discard via a dedicated endpoint:

```
POST /admin/dead-letter/{event_id}/discard
     Body: {"reason": "stale|invalid_payload_unrecoverable|too_old"}
     Sets status to 'discarded'.
     Writes to audit_log with reason.
     Does not delete the row (audit trail must be preserved).
```

---

## Dead Letter Metrics and Alerting

```
radar_outbox_dead_letter_total          counter, increments each time an event dead-letters
radar_outbox_dead_letter_depth          gauge, current count of dead_letter status events
radar_outbox_replays_total              counter, increments each time an event is requeued
radar_outbox_discards_total{reason}     counter, increments each time an event is discarded
```

The `dead_letter_depth` gauge should trend toward zero during normal operations.
If it grows consistently, something structural is broken.

---

## Preventing Dead Letters

Most dead letters are preventable:

**Liveness and readiness probes** ensure the outbox worker only dispatches to
services that are actually ready. If a service's readiness probe fails, Kubernetes
removes it from the service endpoint list and the outbox worker gets connection
refused immediately rather than after a timeout.

**Circuit breaker on the outbox worker** (future improvement) would detect sustained
failures to a specific target service and pause dispatching to it for a backoff
period, rather than burning through all retry attempts.

**Schema validation in the producer** catches invalid payloads before they are
written to the outbox. If ingestion tries to write an `alert.normalized` event with
a missing required field, the validation should fail at write time, not at dispatch
time five retries later.

---

## Decision Record

Dead letters are investigated before replay. Root cause fixed first. Individual
replay, not batch. 4xx events investigated carefully before replay. Stale or
unrecoverable events discarded with audit trail. All replays and discards logged.
Metrics track dead letter depth and rate.
