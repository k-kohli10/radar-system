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
what they watch rather than where they live. See "Alert rules" below for both.

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
│   ├── plugin-sdk/
│   │   ├── src/radar_plugin_sdk/
│   │   │   ├── __init__.py
│   │   │   ├── registry.py
│   │   │   ├── base.py
│   │   │   ├── loader.py
│   │   │   └── config.py
│   │   ├── tests/
│   │   └── pyproject.toml
│   │
│   └── testing/                       # radar-testing: shared pytest fixtures.
│       ├── src/radar_testing/         # Dev dependency only, never a runtime import.
│       │   ├── __init__.py
│       │   └── postgres.py
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
│   ├── knowledge/
│   │   └── elastic/
│   ├── traces/
│   │   └── elastic/                   # Phase 10. Not built yet.
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
│   │   ├── 0012-cd-approach.md
│   │   ├── 0013-watcher-correlation-scope.md
│   │   ├── 0014-event-schema-versioning.md
│   │   ├── 0015-database-migration-rules.md
│   │   ├── 0016-incident-lifecycle-state-machine.md
│   │   ├── 0017-dead-letter-replay.md
│   │   ├── 0018-single-repository.md
│   │   ├── 0019-no-llm-frameworks.md
│   │   └── 0020-static-token-auth.md
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
├── tests/                             # Cross-service tests. Per-service unit
│   ├── e2e/                           # tests live in each app's own tests/.
│   │   ├── harness.py
│   │   ├── test_pipeline.py
│   │   └── ...
│   ├── retrieval/                     # Retrieval probes + scored baselines.
│   │   ├── probes.yaml
│   │   └── baseline*.json
│   └── load/                          # Phase 13. Not built yet.
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
-> resolved`; `closed_at` reserved) are recorded in
[ADR 0016](adr/0016-incident-lifecycle-state-machine.md).

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
  transition gated on delivery
  ([ADR 0016](adr/0016-incident-lifecycle-state-machine.md) Amendment 1).
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
  `transition_status` stamps `closed_at`, but no caller reaches `closed`
  ([ADR 0016](adr/0016-incident-lifecycle-state-machine.md) Amendment 3).
  The bot is read-only in v1; a state-changing command is a different
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

**Prerequisite for step 12 — two incident-lifecycle metrics are defined but never
observed.** `radar_incident_duration_seconds` and `radar_incidents_total` exist in
`radar_telemetry.metrics` (on the `IncidentMetrics` family) but nothing ever
records them: the duration histogram has zero observations and the incidents
counter is never incremented (it is even registered on feedback-service, not
ingestion). So the incident-pipeline dashboard's "Ingestion→recommendation
latency" and "Incidents opened" panels read "No data" — each carries a panel
description saying so. Step 7 leaves them empty by design (config-only).

Before step 12's measurement, land a **dedicated, labeled instrumentation commit
with its own test** (not folded into a config step) that wires the observations:
- ingestion increments `radar_incidents_total{service,severity}` when it opens an
  incident;
- the pipeline observes `radar_incident_duration_seconds` at RECOMMENDATION
  creation, measuring `recommendation.created_at - incident.created_at` — i.e.
  INGESTION-TO-RECOMMENDATION (pipeline) latency, NOT open-to-resolution (which
  would fold in human-loop time). The metric's current help text
  ("open-to-resolution") is misleading and is corrected in that commit.

Only then does step 12 ("measure real p50/p95 off the dashboards, delete the ~50s
placeholder") have data to read.

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
ci: assert otel collector config copies are byte-identical
ci: add cd workflow to home lab
docs: add adr 0012 cd approach
docs: add cluster connectivity setup guide
```

**Deferred from Phase 10 — `deploy/otel/` config drift check.** Two config files
under `deploy/otel/` each exist in two places that MUST match, because compose
needs a mountable file while a static k8s manifest needs the content inline:

- `collector-config.yaml` ↔ the `otel-collector-config` ConfigMap in
  `collector-daemonset.yaml`.
- `traces-index-template.json` ↔ the `traces-index-template` ConfigMap in
  `traces-index-template.yaml`.
- `deploy/fluent-bit/parsers.conf` ↔ the `parsers.conf` key in the
  `fluent-bit-config` ConfigMap in `fluent-bit-daemonset.yaml`. (The two
  `fluent-bit.conf` files legitimately DIFFER — compose tails `.dev-run`, k8s
  tails container logs — so only the parser is a byte-identical pair.)

Phase 10 keeps each pair byte-identical by hand; nothing structural prevents them
drifting silently. Add a CI job that, for each pair, extracts the ConfigMap's
embedded value and asserts it equals the standalone file verbatim, failing the
build on any divergence. Same assert-don't-trust discipline as the
`deploy/`-only-change guard above.

**CI prerequisite — kubeconform needs schema-repo egress.** The Phase 10
kubeconform proof (`make kubeconform` / `tests/e2e/test_kubeconform.py`, in the
default suite via `-m 'not live'`) runs the pinned `ghcr.io/yannh/kubeconform`
image, which fetches the Kubernetes JSON schemas from the `kubernetes-json-schema`
repo (raw.githubusercontent.com) at run time. The CI runner therefore needs Docker
AND network egress to that host; without it the check reports "CANNOT TRUST —
ERROR(s): schema-fetch / network egress" (exit 2), deliberately distinct from a
manifest actually being invalid. If CI must run air-gapped, pre-cache the schemas
and point kubeconform at them with `-schema-location`.

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

**Deferred from Phase 10 — authenticated Alertmanager → ingestion.** Ingestion
authenticates `POST /alerts/prometheus` with the `X-Radar-Webhook-Token` header
(ADR 0011). Alertmanager v0.27 (the compose pin) can send `Authorization` /
basic-auth but not an arbitrary custom header, so `deploy/prometheus/alertmanager.yml`
wires the webhook to ingestion as a dev convenience that 401s (documented at the
receiver). Real authenticated delivery needs an Alertmanager with
`http_config.http_headers` (≥ v0.28); bump the image and set the token header from
the mounted secret when the k8s wiring lands. Phase 10's scrape → fire → webhook
proof does not depend on it — `tests/e2e/test_real_prometheus_alert.py` asserts
the alert reaches a webhook receiver with the right labels.

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
---

## Architecture Decision Records

Full ADRs live in [adr/](adr/). They were previously appended to this
document in full, which duplicated the files and collided with their
numbering; the plan now links them.

- [ADR 0003 — Postgres Transactional Outbox for All Agent Communication](adr/0003-postgres-outbox.md)
- [ADR 0019 — No LangChain, LangGraph, or LiteLLM](adr/0019-no-llm-frameworks.md)  *(was ADR 0004 inline)*
- [ADR 0020 — Static Token Auth for Internal Services in V1](adr/0020-static-token-auth.md)  *(was ADR 0013 inline)*
- [ADR 0014 — Event Schema Versioning Rules](adr/0014-event-schema-versioning.md)
- [ADR 0015 — Database Migration Rules](adr/0015-database-migration-rules.md)
- [ADR 0016 — Incident Lifecycle State Machine](adr/0016-incident-lifecycle-state-machine.md)
- [ADR 0017 — Dead Letter Replay Strategy](adr/0017-dead-letter-replay.md)

See [adr/](adr/) for the complete set, including those never inlined here.
