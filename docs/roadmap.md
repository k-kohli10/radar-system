# 🗺️ Roadmap

RADAR ships **one milestone at a time**. Each milestone is one PR and lands only
after its "Done when" criteria are met, and no milestone starts work reserved for
a later one. The full technical plan, with every deliverable and acceptance
criterion, lives in [docs/implementation_plan.md](implementation_plan.md); this
is just the summary view.

## Contents

- [Milestones](#-milestones)
- [The Proof-of-Concept Line](#-the-proof-of-concept-line)
- [Fixed Decisions](#-fixed-decisions)
- [Carried Debt](#-carried-debt)
- [Development Notes](#-development-notes)

---

## 🗓️ Milestones

| Milestone | Tag | Focus |
|---|---|---|
| `v0.0-foundation` | n/a | Docs and decisions only. No code. |
| `v0.1-dev-env` | n/a | Workspace, Docker Compose stack, tooling. |
| `v0.2-contracts` | n/a | Pydantic contracts and plugin SDK. Zero vendor imports. |
| `v0.3-packages` | n/a | Shared common/database/telemetry packages. Outbox atomicity, poller isolation, idempotency proven by test. |
| `v0.4-llm-gateway` | n/a | LLM Gateway: token IAM, mode routing, retries, provider fallback. |
| `v0.5-ingestion` | n/a | Ingestion API, platform-sim, dedup logic. Six fireable scenarios across four services; alert rules declared in `deploy/prometheus/`. |
| `v0.6-outbox-worker` | n/a | Full outbox worker spec: polling, dispatch, retry, dead letter, graceful shutdown. |
| `v0.7-vertical-slice` | v0.1.0 | Watcher, Planner, and Reasoner working end to end with real LLM calls. First proof of concept. |
| `v0.8-knowledge` | v0.2.0 | Runbooks written, knowledge-service RAG retrieval, CRAG grading. |
| `v0.9-feedback` | v0.3.0 | Feedback-service: RCA delivery cards and Slack bot, in one deployment. |
| `v0.10-observability` | v0.4.0 | Dashboards, alert rules, OTel trace coverage, RADAR's own operations runbooks. Also owns the deferred platform-sim wiring: Prometheus + Alertmanager, their compose services, and the real scrape-to-fire-to-webhook path in the default suite. |
| `v0.11-cicd` | v0.5.0 | Path-based CI, multi-arch builds, per-service CD. |
| `v0.12-kubernetes` | v0.6.0 | Helm chart, RBAC, HPA, ConfigMaps for rules/templates, deployable examples. |
| `v0.13-hardened` | v0.7.0 | Load test, circuit breaker, threat model, audit log completeness. |
| `v1.0` | v1.0.0 | Open-source polish: quickstart, plugin guide, benchmark, house doc style. |

---

## 📏 The Proof-of-Concept Line

Everything through `v0.7-vertical-slice` is the proof of concept: one alert, one
correlated incident, one investigation plan, one LLM-generated (or fallback) RCA,
proven by an end-to-end test with real OpenAI calls. Everything after it, meaning
knowledge retrieval, Slack delivery, observability, CI/CD, Kubernetes, hardening,
and polish, is improvement on top of a working core, not a prerequisite for it.

---

## 🔒 Fixed Decisions

The "Locked Decisions" in the implementation plan hold for every milestone:
Postgres-outbox-only agent comms, Slack-only notifications, Vault
init-container-only secrets, and the stack constraints (no Redis, no Jaeger, no
agent frameworks). These are settled for v1, and each milestone builds on them.

---

## 💳 Carried Debt

Each item is recorded in the milestone that incurs it and paid in the milestone
named in the "Owed by" column.

| Owed by | Item |
|---|---|
| `v0.11-cicd` | **CI must re-enforce the pre-commit checks.** A repo-wide strict `mypy .` pre-commit hook (alongside ruff and gitleaks) was added after a type error in a `tests/` file survived five commits (narrower per-package mypy commands were being run by hand while the Makefile's correct `mypy .` target went uninvoked). Until CI exists, that hook is the only automated guard: `--no-verify` is off-limits on this repo. CI must run `mypy .` + `pytest` + `ruff` on PRs, so the checks are enforced at the CI layer and not merely locally. |
| Unscheduled (define when a consumer needs it) | **`investigation_plans.status` has no lifecycle.** Every plan is stored `'pending'` and nothing ever advances it: no code reads or writes plan status. It is inert-but-stated (the planner's `PLAN_STATUS_PENDING` docstring says so), not a silent gap: "was this plan reasoned over?" is already answered by whether a `recommendations` row exists for the incident. Defining a real `pending → …` transition means first answering what the value should be on a fallback and on the duplicate path (questions the plan does not answer), so it waits until a consumer actually needs the column. |
| Unscheduled (revisit under load) | **A slow reasoner dispatch can stall its batch for up to 90 seconds.** The outbox worker dispatches a claimed batch *sequentially*, and the reasoner's per-target dispatch timeout is 90s because it calls an LLM before it can answer. Ninety, not sixty: 60s is the budget the reasoner *aims* for (past which it abandons the LLM and writes a template-fallback RCA), while 90s is what the worker will actually wait if the reasoner is not merely slow but **gone**. So other events claimed in the same batch as a hung reasoning event wait behind it. At low volume this is invisible: the events are delivered a minute or two later and nothing is lost, because the outbox is durable. But under real load it wants **concurrent dispatch within a batch** (`asyncio.gather` over the claimed events). That change redefines the graceful-shutdown semantics tested today (the un-started tail is asserted left in `processing` for the reaper, which concurrency reframes), so it is real work and deliberately not done yet. |

---

## 🗒️ Development Notes

- **`packages/testing` scales to the agents.** The duplicated real-Postgres
  pytest fixtures were extracted into a `radar-testing` workspace package,
  consumed as a dev dependency so test-only code never enters a runtime import
  surface. The watcher, planner, and reasoner agents share the same fixtures: add
  `radar-testing` to their dev dependencies and import `database_url` / `db` from
  `radar_testing.postgres`. Do not copy the block a fourth time. (The dev-dependency
  cycle, where `packages/database`'s dev deps pull in `radar-testing`, which depends
  on `radar-database`, resolves cleanly in uv, since dev deps are not in the runtime
  graph.)

- **The reasoner's system prompt gives the model an example to copy.** In a live
  run, with an empty `retrieved_context`, the model echoed the prompt's own
  illustrative action ("check order-service error logs in Kibana … filtered by
  status=500") back as a recommended action. Harmless, and correct low-confidence
  behaviour given zero evidence, but a sign the prompt's concrete example leaks into
  output when there is nothing else to cite. Retrieval gives the model real runbook
  content to ground actions in; the prompt's worked example should be revisited so
  it teaches format without supplying content the model parrots.

- **Query specificity for unknown alerts.** No-coverage detection is reliable for
  symptom-rich queries (CRAG gate e2e: 9/9 empty) but boundary-unstable for the
  alert-shaped query an UNKNOWN alert produces (measured 2/5 empty): the `_default`
  plan steps dominate the query, and their generic language ("review latency
  trends") genuinely grazes runbook content, so a `partial` grade is a defensible
  reading rather than a grader bug. The fix is query quality, not grader tuning:
  tightening CRAG's prompt until the boundary case passes would select a config
  because it passes on the probe judging it. Declared hypothesis for when this is
  taken up: weighting service/alert identity over generic plan-step language in
  `build_query` (or raising `_default`'s specificity) moves alert-shaped no-coverage
  queries off the boundary. Judge against the 17-probe set plus the alert-shaped case
  recorded in `tests/retrieval/probes.yaml`, criterion fixed before the change.

- **Correction-gated re-reason (deferred).** The 📝 correction modal was deferred
  because no consumer re-reasons over a correction, so a captured fix would land
  in a `recommendation_feedback` row nobody reads, and `correction_text` stays
  reserved on the schema and the contract, and `InteractionAction` deliberately
  omits the action. What makes the consumer worth building is that the pipeline
  already knows which RCAs are ungrounded and why: CRAG's `empty` verdict is kept
  distinct from `unavailable` the whole way to the stored bundle, so an incident
  whose RCA was written with nothing behind it is identifiable after the fact
  rather than inferred. A correction on one of those is not a per-incident patch
  competing with retrieved context. It is the only ground truth available for an
  incident the corpus does not cover, and it names the runbook that should exist.
  Gating on a correction rather than on 👎 follows from the same reasoning: a bare
  thumbs-down carries nothing to reason differently from. Scope it narrowly,
  though. The sharpest RCA failure measured so far (an all-`partial` bundle
  driving an empty-context RCA with the right runbook in hand, fixed in the prompt
  projection and pinned by `tests/e2e/test_prompt_grade_leak.py`) was systemic,
  and a correction loop would have absorbed it as a stream of plausible one-off
  human fixes instead of surfacing it. This is also the first edge to run the
  pipeline backwards: feedback-service is currently terminal and calls no
  `write_outbox`.
