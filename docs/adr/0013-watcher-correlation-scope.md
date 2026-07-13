# ADR 0013: Ingestion owns incident identity; the watcher owns correlation policy

**Status:** Accepted
**Date:** 2026-07-13
**Phase:** 7 (Agent Pipeline and Vertical Slice)

## Context

The implementation plan's *Watcher Agent Logic* has the watcher compute a
fingerprint, search for an open incident within a window, and then either attach the
alert to it or **INSERT a new incident**. Phase 5's ingestion service, already
shipped and tested, does exactly that same work: it computes
`sha256(service_name:alert_name:severity)`, looks for an open incident inside a
5-minute window, and attaches or opens one — then writes the `alert.normalized`
outbox event the watcher later receives.

So by the time an event reaches the watcher, **the incident and the alert rows
already exist**. The plan's watcher steps cannot execute as written:

- `incidents.correlation_id` is `UNIQUE`, and ingestion already used the ingress
  correlation id for this incident.
- The alert row's primary key is the `NormalizedAlert.id` carried in the event
  payload; re-inserting it is a primary-key violation.

The plan is internally inconsistent here. Its own end-to-end test reads `incident_id`
from ingestion's `202` response — which only works if *ingestion* opens the incident —
and `docs/architecture/sequence-flows.md` describes the shipped behaviour ("Note over
ingestion: normalize, dedupe, INSERT incident+alert+outbox"). It is the *Watcher Agent
Logic* pseudo-code and the agent-pipeline flowchart that are stale.

## Decision

**Ingestion owns incident identity. The watcher owns correlation policy.**

The watcher never inserts an incident or an alert. It loads the incident ingestion
resolved (by `incident_id`, carried on the event), and decides what should *happen*
to it:

- **escalation** — raise its severity when alerts arrive fast enough
- **suppression** — withhold the investigation plan for a too-soon repeat
- otherwise, emit `incident.plan_requested`

Three consequences follow, and each is a deliberate deferral rather than an
oversight.

### 1. Ingestion publishes on the dedup path too

Ingestion previously wrote **no** outbox event when an alert deduplicated onto an open
incident, so duplicates never reached the watcher. That makes escalation — *"3 alerts
within 2 minutes"* — unenforceable by construction, not merely unimplemented: a
watcher shown only first-of-kind alerts can never observe a burst. Ingestion now
publishes on both paths, tagging the payload with `incident_id` and `deduplicated`
(commit `a288a4e`).

### 2. `default_window_minutes` / `window_overrides` are inert

The correlation window is applied wherever the new-vs-duplicate decision is made, and
that is ingestion, which uses a hardcoded 5 minutes. The watcher cannot honour a
different window because it cannot un-create an incident ingestion already opened. A
`window_overrides` entry the watcher "respected" would be a claim it has no power to
make.

Making them live means moving the window decision — either ingestion reads this same
ConfigMap, or the window moves into ingestion's own settings. Both are real options;
neither is needed for the POC, and the boundary is already tested where it is actually
enforced (ingestion's `test_dedup_boundary` pins 4m59s / 5m00s / 5m01s).

### 3. `service_groups` are inert

Folding `[order-service, order-db, inventory-service]` into a single incident requires
**merging incidents ingestion has already opened separately** — a lifecycle the schema
does not have (there is no `merged_into_id`, no `merged` status) and the POC does not
need. The config also contains an ambiguity that proves the feature needs designing
rather than defaulting: `order-service` belongs to *both* `order-stack` and
`checkout-stack`, and nothing says which wins.

### 4. `fingerprint_fields` is a declaration, not a knob — and it is enforced

Ingestion hashes the three fields in code. Rather than let the YAML *appear* to
control something it does not, the watcher **refuses to start** if
`fingerprint_fields` no longer matches what ingestion actually hashes. The field most
likely to be edited in the belief that it changes behaviour therefore cannot diverge
in silence: it fails loudly.

## Consequences

**Good.** Zero change to a shipped, tested phase's write path. The e2e test's
`incident_id` from the `202` stays meaningful. The watcher has a real, testable job
(policy over time) rather than a duplicate of ingestion's. Every rule in the YAML is
validated on every startup, so a typo is a `/readyz` 503, not a silently disabled
feature.

**Bad.** Three of the five rule sections in `correlation-rules.yaml` are inert for the
POC. That is a genuine gap between the config's apparent surface and its behaviour, and
the mitigation is that it is *stated* — in the YAML's own comments, in the loader's
docstring, and by a test that asserts the deferred rules do **not** affect correlation.
An inert field that nobody has written down is a trap; an inert field with a test
pinning it inert is a decision.

**The window mismatch is real and worth naming.** A watcher window *shorter* than
ingestion's (the config's `OrderServiceCrashLoop: 2m`) is unenforceable in principle
today — those alerts are absorbed by ingestion's 5-minute window and arrive tagged
`deduplicated`, not as new incidents. A *longer* one (`CheckoutTimeoutRate: 10m`) means
ingestion opens a second incident where a 10-minute window would have correlated. Both
resolve the same way: by moving the window decision to whoever opens the incident.

## Alternatives considered

**Strip dedup and incident creation out of ingestion** (the plan, taken literally).
Rewrites a shipped and tested phase, and breaks the `202 → incident_id` response that
the plan's own e2e test depends on.

**Watcher merges group siblings.** Truest to `group_as_single_incident`, and the right
answer eventually. Needs a migration (`incidents.merged_into_id`, a `merged` status)
and a merge lifecycle — real work, no POC payoff, and nothing in Phase 7's tests
exercises it.
