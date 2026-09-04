# 🔭 ADR 0008: OTel Traces to Elasticsearch via Kibana APM

## Contents

- [Status](#-status)
- [Context](#-context)
- [Decision](#-decision)
- [Consequences](#-consequences)
- [Comparison](#-comparison)

## 🚦 Status
Accepted

## 🧩 Context
Debugging a single incident's path through an eight-service pipeline needs distributed
tracing: an alert arriving at ingestion, correlating in watcher-agent, planned in
planner-agent, reasoned over in reasoner-agent (including the LLM gateway round trip),
and delivered by feedback-service, all tied together and visualized as one trace. RADAR
already runs Elasticsearch and Kibana for log storage and, from Phase 8, runbook
retrieval.

## ✅ Decision
Every service instruments requests with the OpenTelemetry SDK
(`opentelemetry-sdk`, `opentelemetry-instrumentation-fastapi`), exporting via OTLP/gRPC
to an OTel Collector running as a DaemonSet, which forwards to Elasticsearch. Traces are
viewed in Kibana APM. No Jaeger, no separate tracing UI.

## ⚖️ Consequences
- One observability backend (Elasticsearch/Kibana) serves logs, traces, and (from
  Phase 8) runbook retrieval. That's one system to run, back up, and query in the home
  lab cluster instead of two.
- `correlation_id` (see [docs/architecture/data-model.md](../architecture/data-model.md))
  is the join key between structured logs and OTel spans. A single incident's full
  path is reconstructable in Kibana using just that one value, across both logs and
  traces.
- Kibana APM's feature set for trace visualization is less specialized than Jaeger's
  UI. That's acceptable given RADAR's tracing needs are "reconstruct one incident's
  path," not high-volume distributed systems performance debugging across thousands of
  services.

## 🆚 Comparison

| Alternative | What it's for | Why RADAR skips it |
|---|---|---|
| Jaeger (separate tracing backend) | Specialized trace visualization, high-volume distributed tracing | Would duplicate a capability Elasticsearch/Kibana APM already provides; RADAR already runs Elasticsearch/Kibana for logs, so a second backend means operating two systems instead of one |
