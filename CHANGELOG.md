# 📓 Changelog

RADAR is built **one phase at a time** — each phase is a milestone with its own
"Done when" bar. This file tracks those milestones. 🗺️ The full plan lives in
[docs/implementation_plan.md](docs/implementation_plan.md); the summary view is
[docs/roadmap.md](docs/roadmap.md).

Format follows [Keep a Changelog](https://keepachangelog.com); versions are the
phase milestones (see [🏷️ Versioning](#-versioning)).

---

## 📚 Contents

- [🚀 1.0.0 — Open-Source Polish](#-100--open-source-polish)
- [🧱 Milestone History](#-milestone-history)
- [🏷️ Versioning](#-versioning)

---

## 🚀 1.0.0 — Open-Source Polish

**Phase 14** · the release that makes RADAR approachable from a cold clone.

### ✨ Added
- ⏱️ **15-minute quickstart** — clone → live LLM-generated RCA on one machine ([docs/quickstart.md](docs/quickstart.md)).
- 🔌 **Plugin development guide** — add an LLM / notification / logs / metrics / traces backend without touching a service ([docs/plugin-development.md](docs/plugin-development.md)).
- 📓 **This changelog** and a refreshed **contributor guide** ([CONTRIBUTING.md](CONTRIBUTING.md)).

### 💅 Changed
- 📖 **README** polished for v1.0 — run paths, stack, badges, and honest status.

### 🚧 Planned
- 📊 Performance benchmark from the Phase 13 load test.
- 🧑‍💻 SRE portfolio case study.

---

## 🧱 Milestone History

Phases 0 → 13 — the working system that 1.0 polishes. Each row is one phase, one PR.

| Milestone | Phase | Highlights |
|---|---|---|
| `v0.0-foundation` 🏷️ | 0 | Docs and locked decisions only — no code. |
| `v0.1-dev-env` | 1 | uv workspace, Docker Compose stack, tooling. |
| `v0.2-contracts` | 2 | Pydantic contracts + plugin SDK. **Zero vendor imports** enforced by CI. |
| `v0.3-packages` | 3 | Shared common / database / telemetry. Outbox atomicity, poller isolation, idempotency — each proven by test. |
| `v0.4-llm-gateway` | 4 | LLM gateway: token IAM, mode routing, retries, provider fallback. |
| `v0.5-ingestion` | 5 | Ingestion API, platform-sim, 5-min dedup window; six fireable scenarios across four services. |
| `v0.6-outbox-worker` 🏷️ | 6 | Full outbox worker: polling, dispatch, retry, dead-letter, graceful shutdown. |
| `v0.7-vertical-slice` | 7 | Watcher + Planner + Reasoner end to end with real LLM calls. **First POC.** |
| `v0.8-knowledge` | 8 | Runbooks, knowledge-service RAG retrieval, CRAG grading. |
| `v0.9-feedback` | 9 | Feedback-service: RCA delivery cards + Slack bot. |
| `v0.10-observability` | 10 | Dashboards, alert rules, OTel trace coverage, operations runbooks. |
| `v0.11-cicd` | 11 | Path-based CI, multi-arch builds, per-service CD. |
| `v0.12-kubernetes` | 12 | Helm chart, RBAC, HPA, ConfigMaps for rules/templates. |
| `v0.13-hardened` | 13 | Load test, circuit breaker, threat model, audit-log completeness. |

🏷️ = a real git tag today. See below.

---

## 🏷️ Versioning

- 📌 **Milestones are the version scheme.** Every phase has a milestone name
  (`v0.x-<focus>`); `1.0.0` is the first public release.
- 🔖 **Only two milestones are git-tagged so far** — `v0.0-foundation` and
  `v0.6-outbox-worker`. The rest are milestone names, not tags.
- 🚫 **No `v0.1.0`–`v0.7.0` semver tags exist.** The roadmap lists them as the
  *intended* scheme; they were never cut, so this changelog doesn't claim them.
