# radar-telemetry

Observability primitives shared by every RADAR service:

- **metrics** — Prometheus metric factories for the platform-wide, LLM, outbox,
  and incident-pipeline metrics, exposed at `/metrics`
- **tracing** — OpenTelemetry tracer setup exporting via OTLP/gRPC to the OTel
  Collector (forwarded to Elasticsearch, viewed in Kibana APM), plus FastAPI
  request instrumentation
- **events** — helpers to annotate the current span with domain events and to
  thread `correlation_id` — the join key between structured logs and traces

See docs/adr/0008-otel-to-elasticsearch.md.
