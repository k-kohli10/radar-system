# 🔐 Threat Model

## Contents

- [Scope](#scope)
- [System Model and Trust Boundaries](#system-model-and-trust-boundaries)
- [Assets](#assets)
- [Threats and Mitigations](#threats-and-mitigations)
  - [B1: External alert sources → ingestion](#b1-external-alert-sources--ingestion)
  - [B2: Agent → LLM gateway → provider](#b2-agent--llm-gateway--provider)
  - [B3: Untrusted content in the reasoning path](#b3-untrusted-content-in-the-reasoning-path)
  - [B4: Secrets and Vault](#b4-secrets-and-vault)
  - [B5: Slack](#b5-slack)
  - [B6: Postgres and the audit log](#b6-postgres-and-the-audit-log)
  - [B7: Availability under load](#b7-availability-under-load)
- [Residual Risks](#residual-risks)
- [Out of Scope for V1](#out-of-scope-for-v1)

## Scope

This models RADAR itself: the ingestion, agent, gateway, knowledge, and
feedback services and the data flowing between them. It does **not** model the
target e-commerce platform RADAR observes, the detection systems upstream
(Prometheus, Kibana Watcher), or the managed Kubernetes / Vault / Postgres /
Elasticsearch platform RADAR runs on, except where RADAR's trust in them is a
boundary of its own (B4, B6). RADAR is an internal SRE tool: the adversaries
worth modelling are a compromised or misconfigured neighbour inside the cluster,
a forged or malicious inbound alert, and untrusted text riding an alert into the
LLM prompt. It does not model an anonymous internet attacker, because nothing here is meant to
be internet-facing except the two alert webhooks (B1).

## System Model and Trust Boundaries

```
             ┌── B1 ──┐                                    ┌── B4 ──┐
 Prometheus ─┤        │                                    │ Vault  │
 Kibana ─────┤ webhook│                                    └───┬────┘
 (mock) ─────┤ token  │                                        │ init-container
             └───┬────┘                                        │ files (never env)
                 ▼                                             ▼
            ┌─────────┐   outbox    ┌─────────┐  outbox  ┌──────────┐
            │ingestion├────(DB)────►│ watcher ├──(DB)───►│ planner  │
            └────┬────┘             └─────────┘          └────┬─────┘
                 │ INSERT incident/alert/audit                │ outbox (DB)
                 ▼                                            ▼
            ┌─────────────── Postgres (B6) ──────────────────────────┐
            │ incidents · alerts · plans · recommendations · feedback │
            │ outbox_events · processed_events · audit_log (append-only)│
            └─────────────────────────┬───────────────────────────────┘
                                      │ outbox (DB)
                                      ▼
                                 ┌──────────┐   X-Radar-Agent-Token   ┌─────────┐   B2   ┌──────────┐
                                 │ reasoner ├────────(HTTP)──────────►│ gateway ├──(TLS)►│ LLM      │
                                 └────┬─────┘   one token = one mode  └─────────┘        │ provider │
                                      │ outbox (DB)                                      └──────────┘
                                      ▼
                                 ┌───────────────┐   B5   ┌────────┐
                                 │ feedback-svc  │◄─socket►│ Slack  │
                                 └───────────────┘ (xapp/  └────────┘
                                                    xoxb)
```

RADAR's services never call each other directly: every hand-off is a row in
`outbox_events`, claimed `FOR UPDATE SKIP LOCKED` by the outbox-worker and
delivered over HTTP with an internal agent token. The trust boundaries an
attacker would cross are numbered B1–B7 above and analysed below.

## Assets

| Asset | Why it matters |
|-------|----------------|
| Incident / recommendation data in Postgres | The system of record; corruption or loss means wrong or missing RCAs at 3am |
| The **audit log** | Append-only forensic trail; its integrity is what makes every other claim ("this transition happened") trustworthy |
| LLM provider API keys | Spend and data-exfiltration risk if stolen |
| Internal agent tokens | One token = one gateway mode; a leaked token can run up an LLM bill or reach the gateway |
| Webhook tokens (per source) | Gate the only inbound endpoints; a leak lets an attacker forge alerts |
| Slack app/bot tokens | Post to the workspace and read interactions |
| Vault | Root of trust for every secret above |

## Threats and Mitigations

Organised per boundary; each threat is tagged with the STRIDE category it falls
under and names the mitigation actually in the codebase (with the ADR or module
that owns it), then the residual risk.

### B1: External alert sources → ingestion

The only endpoints RADAR intends to expose outside its own trust boundary:
`POST /alerts/{prometheus,kibana,mock}`.

- **Spoofing (a forged alert opens a bogus incident).** Every alert endpoint
  requires `X-Radar-Webhook-Token`, a per-source token distinct from the internal
  agent token (ADR 0011), fail-closed: missing/wrong → 401, and a source whose
  token file is absent has its endpoint disabled entirely. Per-source tokens mean
  compromising Prometheus's credential never authenticates as Kibana's.
- **Tampering / DoS (a malformed or batched body crashes the handler).** Ingestion
  normalizes exactly one alert per request; a batched `alerts` array or any
  malformed payload is rejected **422** (`InvalidPayloadError`), never truncated to
  `alerts[0]` and never crashing (ADR 0011). Unknown fields are dropped by the
  typed contracts, not reflected onward.
- **Repudiation (was this alert really received?).** Every opened incident and every
  deduplicated attach writes an `audit_log` row in the same transaction
  (`ingestion.incident_opened` / `ingestion.alert_attached`); a resolve that
  matched nothing writes `ingestion.resolve_ignored`. There is no un-audited
  ingestion outcome.
- *Residual:* a source that leaks its own webhook token can forge alerts until it
  is rotated (rotate the one Vault file, restart ingestion; ADR 0011). Rate
  limiting per source is not implemented (B7).

### B2: Agent → LLM gateway → provider

- **Spoofing / Elevation (a compromised service runs up an LLM bill or reaches a
  mode it should not).** Internal calls carry `X-Radar-Agent-Token`, one static
  64-hex token per service, Vault-stored (ADR 0020). At the gateway each token maps
  to **exactly one mode**, and a service holding two capabilities holds two tokens,
  not one token granting both: the reasoner's token can only make `extended` calls,
  and the knowledge-service's embed token cannot be spent on the order-of-magnitude
  costlier `reason` mode its separate token grants. (The watcher and planner are
  rule- and template-based and hold no gateway grant at all.) Validation order fails
  fast: unknown token → 401, wrong mode for token → 403, over the input budget → 422
  (LLM Gateway spec). Token comparison is constant-time (`hmac.compare_digest` over
  every entry, no early return) so a valid token cannot be recovered by timing.
- **Information disclosure (secrets or prompt content leak through logs or errors).**
  The gateway's "what never gets logged" rule is enforced by construction: message
  content, API keys, agent tokens, and raw LLM response bodies never enter a log
  line; only mode, provider, model, token counts, latency, and status do. Vendor SDK
  exceptions are redacted to their **class name** at the provider boundary and
  re-raised `from None`, so a provider error body (which can echo prompt content)
  never rides into a log or a 5xx detail.
- **Egress control (an agent talks to a provider directly).** Only the gateway
  holds provider API keys and only the gateway imports vendor SDKs (behind the
  plugin boundary); no code in `apps/` or `packages/` can reach a provider. The
  gateway→provider hop is the single egress point, over TLS to the vendor.
- *Residual:* a stolen agent token is usable (for its one mode) until rotated; V1
  chooses static tokens over short-TTL JWT deliberately (ADR 0020), accepting this
  in exchange for no token-service dependency.

### B3: Untrusted content in the reasoning path

The subtle boundary: **alert labels/annotations and runbook text are attacker- or
author-influenced text that flows into an LLM prompt.** A crafted alert annotation
could attempt prompt injection ("ignore your instructions; recommend running…").

- The reasoner's system prompt treats `alert_labels`, `alert_annotations`, and
  `retrieved_context` as **evidence, not instructions**, and pins hard rules: do
  not hallucinate metrics, log lines, or deployment names not given; never invent
  or cite a runbook that was not retrieved. The output is constrained to a strict
  JSON schema (`root_cause`, `confidence`, `recommended_actions`) and parsed; an
  unparseable or off-schema answer falls back to the template RCA rather than being
  passed through.
- Recommendations are **advisory**: investigation steps and a root-cause
  assessment delivered to a human. RADAR executes nothing it recommends; there is
  no path from a model's `recommended_actions` to an automated action, so a
  successful injection yields bad advice a human reads, not code that runs.
- Grade/status bookkeeping is kept out of the prompt by a field **whitelist**
  (`PROMPTED_CHUNK_FIELDS`), so the model is shown only `title/runbook_id/section/
  content` of a retrieved chunk. A new field reaches the model only when someone
  adds it to the tuple.
- *Residual:* prompt injection cannot be fully prevented for a free-text model.
  The mitigation is blast-radius: advisory-only output, schema-constrained parsing,
  and a human in the loop. RADAR does not sanitise alert text beyond typing it.

### B4: Secrets and Vault

- **Information disclosure (secrets in env vars, images, or git).** Every secret is
  a **file** delivered by the Vault init-container (ADR 0007); nothing reads a
  secret from an environment variable or a baked-in image layer. Dev platform
  credentials are generated in-cluster, not committed (Phase 12). Each secret is
  its own file so rotating one (rotate in Vault, restart the pod) never rewrites the
  others.
- **Tampering (a config error exposes a token in a log).** Config/secret loaders
  are written never to embed a token value in an error: YAML parse failures of the
  token map are reported without the underlying exception text, and `TokenMap.repr`
  shows only service names.
- *Residual:* Vault itself, and the platform's protection of the init-container
  mount, are out of scope (trusted platform). A root compromise of the node reads
  the mounted files; RADAR's boundary ends at "secrets are files, not env."

### B5: Slack

RADAR connects to Slack over **Socket Mode**: an outbound WebSocket authenticated
by an app token (`xapp`) and a bot token (`xoxb`), both Vault-stored. There is **no
inbound public Slack webhook** to forge; RADAR exposes no HTTP endpoint to the
internet for Slack.

- **Spoofing (a forged interaction triggers a state change).** Interactions arrive
  only over the authenticated socket RADAR opened; the handler acts on a closed set
  of known `action_id`s and looks the referenced recommendation up in Postgres, so
  a click can only reference a real recommendation. Incident state transitions go
  through the lifecycle state machine, which rejects illegal edges and writes an
  `incident.invalid_transition` audit row for a rejected attempt.
- *Residual:* anyone in the connected Slack workspace can click a feedback button;
  RADAR does not currently authorize *which* workspace user may resolve an incident
  (feedback is attributed by `slack_user_id`, not gated by it).

### B6: Postgres and the audit log

- **Tampering (the audit trail is rewritten to hide an action).** `audit_log` is
  append-only by discipline: no code path updates or deletes a row, and every audit
  row is written in the **same transaction** as the state change it records, so a
  rolled-back change leaves no orphaned "it happened" row and a committed change
  always leaves one. Phase 13 verifies with teeth that one alert produces one audit
  row per pipeline stage (`tests/e2e/test_audit_trail.py`).
- **Tampering / repudiation (a replayed or duplicated event double-writes).** Every
  agent checks `processed_events` before handling an event and inserts its marker in
  the same transaction; a concurrent duplicate that races the check hits the PK and
  is absorbed as a no-op. The outbox `FOR UPDATE SKIP LOCKED` claim means two
  workers never deliver the same event.
- *Residual:* a principal with direct write access to Postgres can tamper with any
  table including `audit_log`; RADAR relies on the platform to restrict DB
  credentials to the services. Append-only is enforced in code, not by a DB
  privilege / trigger.

### B7: Availability under load

- **DoS (a provider outage stalls every incident).** The LLM gateway's **circuit
  breaker** (Phase 13) opens after repeated failures to a provider binding and fails
  fast to the fallback binding, instead of every request burning its full retry
  budget on a dead provider. When all providers are exhausted the reasoner writes a
  **template RCA** (`is_fallback=true`) so an incident is *never* left without a
  recommendation.
- **DoS (a burst of alerts loses data or wedges the pipeline).** The outbox is a
  durable queue with bounded retries and a dead-letter terminus; the Phase 13 load
  test fires 100 concurrent alerts and asserts no data loss (100 incidents → 100
  recommendations, outbox drained, nothing dead-lettered).
- *Residual:* there is no per-source inbound rate limit on the alert webhooks, and
  no global admission control; a source that floods `/alerts/*` can grow the outbox
  backlog (visible on the outbox-depth panel, runbook in `docs/operations/`).

## Residual Risks

Collected from the per-boundary analysis, the risks V1 knowingly accepts:

1. **Static tokens over short-TTL JWT** (B2): a leaked token is valid until
   rotated; chosen for operational simplicity (ADR 0020).
2. **Prompt injection is bounded, not eliminated** (B3): mitigated by
   advisory-only, schema-constrained output and a human in the loop.
3. **No inbound rate limiting** (B1, B7): a source with a valid token can flood.
4. **Audit append-only is enforced in code, not by DB privilege** (B6).
5. **No per-user authorization on Slack actions** (B5).

## Out of Scope for V1

The target platform, the upstream detection systems, and the managed platform
(Vault, Postgres, Elasticsearch, Kubernetes, the LLM provider) are trusted
dependencies, not modelled here. Network-policy micro-segmentation between RADAR
services, and anomaly detection on RADAR's own traffic, are non-goals for V1:
RADAR authenticates every hop rather than relying on network position.
