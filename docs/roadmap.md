# Roadmap

RADAR is built one phase at a time. Each phase is one PR, one milestone, and lands only
after its "Done when" criteria are met. No phase starts work reserved for a later phase.
Full deliverables, commit sequences, and acceptance criteria for every phase live in
[docs/implementation_plan.md](implementation_plan.md). This is just the summary view.

| Phase | Milestone | Tag | Focus |
|---|---|---|---|
| 0 | v0.0-foundation | n/a | Docs and decisions only. No code. |
| 1 | v0.1-dev-env | n/a | Workspace, Docker Compose stack, tooling. |
| 2 | v0.2-contracts | n/a | Pydantic contracts and plugin SDK. Zero vendor imports. |
| 3 | v0.3-packages | n/a | Shared common/database/telemetry packages. Outbox atomicity, poller isolation, idempotency proven by test. |
| 4 | v0.4-llm-gateway | n/a | LLM Gateway: token IAM, mode routing, retries, provider fallback. |
| 5 | v0.5-ingestion | n/a | Ingestion API, order-stub, dedup logic. |
| 6 | v0.6-outbox-worker | n/a | Full outbox worker spec: polling, dispatch, retry, dead letter, graceful shutdown. |
| 7 | v0.7-vertical-slice | v0.1.0 | Watcher, Planner, and Reasoner working end to end with real LLM calls. First POC. |
| 8 | v0.8-knowledge | v0.2.0 | Runbooks written, knowledge-service RAG retrieval, CRAG grading. |
| 9 | v0.9-feedback | v0.3.0 | Feedback-service: RCA delivery cards and Slack bot, in one deployment. |
| 10 | v0.10-observability | v0.4.0 | Dashboards, alert rules, OTel trace coverage, RADAR's own operations runbooks. |
| 11 | v0.11-cicd | v0.5.0 | Path-based CI, multi-arch builds, per-service CD. |
| 12 | v0.12-kubernetes | v0.6.0 | Helm chart, RBAC, HPA, ConfigMaps for rules/templates, deployable examples. |
| 13 | v0.13-hardened | v0.7.0 | Load test, circuit breaker, threat model, audit log completeness. |
| 14 | v1.0 | v1.0.0 | Open-source polish: quickstart, plugin guide, benchmark, case study. |

## Phase 7 Is the Line

Everything through Phase 7 (the vertical slice) is the proof of concept: one alert, one
correlated incident, one investigation plan, one LLM-generated (or fallback) RCA,
proven by an end-to-end test with real OpenAI calls. Everything after Phase 7, meaning
knowledge retrieval, Slack delivery, observability, CI/CD, Kubernetes, hardening, and
polish, is improvement on top of a working core, not a prerequisite for it.

## What Doesn't Move

The "Locked Decisions" in the implementation plan (no Redis, no Jaeger, no agent
frameworks, Postgres-outbox-only agent comms, Slack-only notifications, Vault
init-container-only secrets, etc.) hold for every phase. A phase does not get to
reintroduce something the plan already ruled out.

## Carried Debt

Recorded when incurred, paid in the phase named. A phase does not get to quietly
inherit these.

| Owed by | Item |
|---|---|
| Phase 11 (CI/CD) | **CI must re-enforce the pre-commit checks.** Phase 6 added a repo-wide strict `mypy .` pre-commit hook (alongside ruff and gitleaks) after a type error in a `tests/` file survived five commits — narrower per-package mypy commands were being run by hand while the Makefile's correct `mypy .` target went uninvoked. But there is no CI yet, so **that hook is currently the only automated guard**: `--no-verify` is off-limits on this repo until CI exists. Phase 11's CI must run `mypy .` + `pytest` + `ruff` on PRs, so the checks are enforced at the CI layer and not merely locally. Belt-and-suspenders only becomes real then. |
| Unscheduled (only if hot rotation is ever needed) | **Hot rotation causes transient-401 dead-lettering.** Phase 7 gave each service its own agent token and the outbox worker a `dispatch_tokens` map of them, so `make rotate SERVICE=x` writes two Vault secrets and both pods must restart to converge. In the window between those restarts the worker sends the old token and the target rejects it — and a 401 is classified `permanent` by the dispatcher, so the event is **dead-lettered immediately, never retried** (recoverable only via `POST /admin/dead-letters/{event_id}/requeue`). **The default procedure is therefore rotate-on-drained-pipeline: check outbox depth first, rotate only when it is empty.** The fix, deferred until hot rotation is actually needed, is two-phase `[old, new]` acceptance — the target accepts both tokens while the roll proceeds, then drops the old one. `AgentTokenAuth` already takes a list of valid tokens, so the change is cheap when it is wanted; it is simply not worth the operational complexity for a pipeline that can be drained. |

## Notes for Later Phases

- **`packages/testing` scales to the agents.** Phase 6 extracted the duplicated
  real-Postgres pytest fixtures into a `radar-testing` workspace package, consumed
  as a dev dependency so test-only code never enters a runtime import surface.
  Phase 7's watcher, planner, and reasoner agents need the same fixtures: add
  `radar-testing` to their dev dependencies and import `database_url` / `db` from
  `radar_testing.postgres`. Do not copy the block a fourth time. (The dev-dependency
  cycle — `packages/database`'s dev deps pull in `radar-testing`, which depends on
  `radar-database` — resolves cleanly in uv, since dev deps are not in the runtime
  graph.)
