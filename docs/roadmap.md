# Roadmap

RADAR is built one phase at a time. Each phase is one PR, one milestone, and lands only
after its "Done when" criteria are met. No phase starts work reserved for a later phase.
Full deliverables, commit sequences, and acceptance criteria for every phase live in
[docs/implementation_plan.md](implementation_plan.md) — this is the summary view.

| Phase | Milestone | Tag | Focus |
|---|---|---|---|
| 0 | v0.0-foundation | — | Docs and decisions only. No code. |
| 1 | v0.1-dev-env | — | Workspace, Docker Compose stack, tooling. |
| 2 | v0.2-contracts | — | Pydantic contracts and plugin SDK. Zero vendor imports. |
| 3 | v0.3-packages | — | Shared common/database/telemetry packages. Outbox atomicity, poller isolation, idempotency proven by test. |
| 4 | v0.4-llm-gateway | — | LLM Gateway: token IAM, mode routing, retries, provider fallback. |
| 5 | v0.5-ingestion | — | Ingestion API, order-stub, dedup logic. |
| 6 | v0.6-outbox-worker | — | Full outbox worker spec: polling, dispatch, retry, dead-letter, graceful shutdown. |
| 7 | v0.7-vertical-slice | v0.1.0 | Watcher → Planner → Reasoner working end-to-end with real LLM calls. First POC. |
| 8 | v0.8-knowledge | v0.2.0 | Runbooks written, knowledge-service RAG retrieval, CRAG grading. |
| 9 | v0.9-feedback | v0.3.0 | Feedback-service: RCA delivery cards + Slack bot, in one deployment. |
| 10 | v0.10-observability | v0.4.0 | Dashboards, alert rules, OTel trace coverage, RADAR's own operations runbooks. |
| 11 | v0.11-cicd | v0.5.0 | Path-based CI, multi-arch builds, per-service CD. |
| 12 | v0.12-kubernetes | v0.6.0 | Helm chart, RBAC, HPA, ConfigMaps for rules/templates, deployable examples. |
| 13 | v0.13-hardened | v0.7.0 | Load test, circuit breaker, threat model, audit log completeness. |
| 14 | v1.0 | v1.0.0 | Open-source polish: quickstart, plugin guide, benchmark, case study. |

## Phase 7 Is the Line

Everything through Phase 7 (the vertical slice) is the proof of concept: one alert, one
correlated incident, one investigation plan, one LLM-generated (or fallback) RCA — proven
by an end-to-end test with real OpenAI calls. Everything after Phase 7 — knowledge
retrieval, Slack delivery, observability, CI/CD, Kubernetes, hardening, polish — is
improvement on top of a working core, not a prerequisite for it.

## What Doesn't Move

The "Locked Decisions" in the implementation plan (no Redis, no Jaeger, no agent
frameworks, Postgres-outbox-only agent comms, Slack-only notifications, Vault
init-container-only secrets, etc.) hold for every phase. A phase does not get to
reintroduce something the plan already ruled out.