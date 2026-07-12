# ADR 0005: Plugin Architecture for Backends

## Status
Accepted

## Context
RADAR depends on several categories of external backend that a given deployment might
want to swap: LLM providers (OpenAI/Anthropic/Gemini), log storage (Elasticsearch),
metrics (Prometheus), traces (Elasticsearch APM), and notifications (Slack). Someone
running RADAR against a different logging backend, or with only Anthropic access,
should not need to fork application code to do it. At the same time, `packages/contracts`
and `packages/plugin-sdk`, the interfaces these backends implement, must have zero
dependency on any specific vendor SDK, so the core codebase stays swappable in
principle even where only one backend is implemented today.

## Decision
Every swappable backend category is defined as a `Protocol` interface in
`packages/contracts` (`LLMProvider`, `EmbeddingProvider`, `LogsBackend`,
`MetricsBackend`, `TracesBackend`, `NotificationBackend`, `KnowledgeStore`). Concrete
implementations live under `plugins/<category>/<vendor>/` (e.g. `plugins/llm/openai/`,
`plugins/notifications/slack/`) and are loaded by a config-driven registry in
`packages/plugin-sdk`, which checks protocol conformance at registration time. Services
depend only on the protocol type, never on a concrete vendor import.

## Consequences
- `packages/contracts` and `packages/plugin-sdk` have zero vendor imports, enforced by
  a mypy-strict CI check (Phase 2's "done when"). This is what makes the plugin
  boundary real rather than aspirational.
- Adding a new backend (e.g. a Datadog metrics plugin) means writing one new plugin
  package against an existing protocol, not touching the services that consume it.
- The registry's conformance check catches a plugin that doesn't fully implement its
  protocol at load time, not at first use in production.
- Only one implementation per category ships in early phases (OpenAI, Elasticsearch,
  Prometheus, Slack). The architecture is proven by the LLM category having three
  interchangeable providers from Phase 4 onward, not by breadth across every category.
- The loader/registry path itself is proven end-to-end by the LLM gateway (Phase 4),
  which registers the OpenAI/Anthropic/Gemini plugins into a `PluginRegistry` and
  resolves providers per mode through `BackendLoader`. The logs and metrics categories
  reuse that same, already-exercised machinery; they simply have no consumer yet.
- As of Phase 5, `LogsBackend` and `MetricsBackend` are implemented (`plugins/logs/elastic`,
  `plugins/metrics/prometheus`) but **currently unconsumed**: no composition root registers
  or loads them from config. Their conformance (`issubclass` + registry registration) and
  behavior (per-plugin unit tests) are proven, but no running service constructs them from
  config yet. This is deliberate build-ahead, consistent with the one-implementation-per-
  category stance above, not an oversight.
- OPEN QUESTION (`MetricsBackend`): it has no planned consumer. Services emit metrics
  directly through `radar_telemetry` (typed `RequestMetrics`/`LLMMetrics`/`OutboxMetrics`/
  `IncidentMetrics` over `prometheus_client`), which does not sit on `MetricsBackend`. A
  later, deliberate decision should choose whether `radar_telemetry` should be backed by
  `MetricsBackend` (making it the recording seam) or whether `MetricsBackend` stays a
  defined-but-dormant Protocol kept for optionality. Not a blocker; flagged so the dormant
  state is a known choice rather than a surprise.
