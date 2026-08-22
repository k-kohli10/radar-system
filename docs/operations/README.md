# 🛠️ RADAR operations runbooks

Runbooks for **operating RADAR itself**: what an on-call engineer does when a
RADAR service-health alert fires or a secret needs rotating.

These are distinct from [`docs/runbooks/`](../runbooks/), which is the
*simulated-shop* corpus the knowledge-service indexes and the reasoner retrieves
against. Those describe incidents in the demo e-commerce system; these describe
RADAR's own failure modes.

Each runbook maps to the RADAR service-health alerts in
[`deploy/prometheus/radar-service-alerts.yml`](../../deploy/prometheus/radar-service-alerts.yml)
and reads off the Grafana dashboards in [`deploy/grafana/`](../../deploy/grafana/). How
those alerts, dashboards, traces, and logs fit together (and why RADAR self-alerts go to
a blackhole instead of becoming incidents) is drawn in
[`docs/architecture/observability.md`](../architecture/observability.md).

| Runbook | Alert | Dashboard |
|---|---|---|
| [LLM gateway failure](llm-gateway-failure.md) | `LLMTemplateFallbackActive` | `llm-gateway`, `incident-pipeline` |
| [Outbox backlog](outbox-backlog.md) | `OutboxBacklogHigh` | `outbox-health` |
| [Vault secret rotation](vault-secret-rotation.md) | N/A (procedure; a botched rotation *surfaces* as the two alerts above) | `radar-overview` |
