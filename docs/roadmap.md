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
| 5 | v0.5-ingestion | n/a | Ingestion API, platform-sim, dedup logic. Simulator extended pre-Phase-8 to six fireable scenarios across four services; alert rules declared in `deploy/prometheus/`. |
| 6 | v0.6-outbox-worker | n/a | Full outbox worker spec: polling, dispatch, retry, dead letter, graceful shutdown. |
| 7 | v0.7-vertical-slice | v0.1.0 | Watcher, Planner, and Reasoner working end to end with real LLM calls. First POC. |
| 8 | v0.8-knowledge | v0.2.0 | Runbooks written, knowledge-service RAG retrieval, CRAG grading. |
| 9 | v0.9-feedback | v0.3.0 | Feedback-service: RCA delivery cards and Slack bot, in one deployment. |
| 10 | v0.10-observability | v0.4.0 | Dashboards, alert rules, OTel trace coverage, RADAR's own operations runbooks. Also owns the deferred platform-sim wiring: running Prometheus + alertmanager, their compose services, and the real scrape→fire→webhook path in the default suite (proven once opt-in at Phase 5, behind the `infra` marker). |
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

| Unscheduled (define when a consumer needs it) | **`investigation_plans.status` has no lifecycle.** Phase 7 stores every plan `'pending'` and nothing ever advances it — no code reads or writes plan status. It is inert-but-stated (the planner's `PLAN_STATUS_PENDING` docstring says so), not a silent gap: "was this plan reasoned over?" is already answered by whether a `recommendations` row exists for the incident. Defining a real `pending → …` transition means first answering what the value should be on a fallback and on the duplicate path — questions the plan does not, so it waits until a consumer (Phase 9 feedback, Phase 10 dashboard) actually needs the column. |
| Unscheduled (revisit under load) | **A slow reasoner dispatch can stall its batch for up to 90 seconds.** The outbox worker dispatches a claimed batch *sequentially*, and the reasoner's per-target dispatch timeout is 90s because it calls an LLM before it can answer. Ninety, not sixty: 60s is the budget the reasoner *aims* for (past which it abandons the LLM and writes a template-fallback RCA), while 90s is what the worker will actually wait if the reasoner is not merely slow but **gone**. So watcher and planner events claimed in the same batch as a hung reasoning event wait behind it. At POC volume this is invisible — the events are delivered a minute or two later and nothing is lost, because the outbox is durable — but under real load it wants **concurrent dispatch within a batch** (`asyncio.gather` over the claimed events). That change redefines Phase 6's graceful-shutdown semantics (its tests assert the un-started tail is left in `processing` for the reaper, which concurrency reframes), so it is real work and deliberately not done for the POC. |

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

- **Phase 8: the reasoner's system prompt gives the model an example to copy.** In the
  live POC run, with an empty `retrieved_context`, the model echoed the prompt's own
  illustrative action ("check order-service error logs in Kibana … filtered by
  status=500") back as a recommended action. Harmless — and correct low-confidence
  behaviour given zero evidence — but a sign the prompt's concrete example leaks into
  output when there is nothing else to cite. Phase 8's retrieval should give the model
  real runbook content to ground actions in; revisit the prompt's worked example then so
  it teaches format without supplying content the model parrots.

- **Query specificity for unknown alerts (pre-registered, from Phase 8).** No-coverage
  detection is reliable for symptom-rich queries (CRAG gate e2e: 9/9 empty) but
  boundary-unstable for the alert-shaped query an UNKNOWN alert produces (measured
  2/5 empty): the `_default` plan steps dominate the query, and their generic language
  ("review latency trends") genuinely grazes runbook content, so a `partial` grade is a
  defensible reading rather than a grader bug. The fix is query quality, NOT grader
  tuning — tightening CRAG's prompt until the boundary case passes would select a
  config because it passes on the probe judging it. Declared hypothesis for when this
  is taken up: weighting service/alert identity over generic plan-step language in
  `build_query` (or raising `_default`'s specificity) moves alert-shaped no-coverage
  queries off the boundary. Judge against the 17-probe set PLUS the alert-shaped case
  recorded in `tests/retrieval/probes.yaml`, criterion fixed before the change.
