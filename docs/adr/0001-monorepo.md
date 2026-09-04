# 🗂️ ADR 0001: Monorepo for radar-system

## Contents

- [Status](#-status)
- [Context](#-context)
- [Decision](#-decision)
- [Consequences](#-consequences)

## 🚦 Status
Superseded by ADR 0018 (2026-08-05)

## 🧩 Context
RADAR is made up of eight services (ingestion, llm-gateway, outbox-worker,
watcher-agent, planner-agent, reasoner-agent, knowledge-service, feedback-service),
five shared packages, and a plugin layer, all versioned and evolved together during
active development by a single contributor. Platform configuration (Helm values for
Postgres/Elasticsearch/Kibana/Prometheus/Grafana/Vault, dashboards, alert rules,
collector configs) is a different kind of artifact with a different release cadence.
It changes when platform dependencies change, not when application code changes.

## ✅ Decision
Two repositories:

- **radar-system**, the product monorepo. All app code (`apps/`), shared packages
  (`packages/`), the plugin SDK and plugin implementations (`plugins/`), the Helm chart
  for the application itself, docs, and tests.
- **radar-infra**, config only. Helm values for platform dependencies, Grafana
  dashboard JSON, Prometheus alerting rules, OTel Collector config, Fluent Bit config.
  Mostly YAML pointing at community Helm charts.

Within radar-system, CI is path-based: a change to one service's directory triggers
that service's build/test/deploy, not a full-repo rebuild.

## ⚖️ Consequences
- Cross-service refactors (e.g. changing a shared contract in `packages/contracts`)
  are a single PR and a single commit history, not a multi-repo coordination problem.
- `packages/` enforces consistency across agents. One version of `radar_contracts` and
  one version of `radar_database` are used by every service.
- Platform config changes (bumping the Postgres chart version, editing a Grafana
  dashboard) never trigger an application CI run, and vice versa.
- The monorepo requires path-based CI (see
  [ADR 0012](0012-cd-approach.md)) to avoid rebuilding every service on every commit.
