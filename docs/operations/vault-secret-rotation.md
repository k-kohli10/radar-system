# 🔐 Runbook: Vault secret rotation

Not an alert — a **procedure**. But a *botched* rotation surfaces minutes later as
`OutboxBacklogHigh`, `LLMTemplateFallbackActive`, or a stuck `/readyz` 503.

## Contents

- 🎯 [The one principle](#the-one-principle)
- ♻️ [Restart-set per secret type](#restart-set-per-secret-type)
- ✔️ [Verify](#verify)
- 🆘 [If recovery doesn't work / known limits / when to escalate](#if-recovery-doesnt-work--known-limits--when-to-escalate)

## The one principle
Secrets are loaded **once, at startup** (in each service's lifespan). There is
**no hot-reload.** Rotating a secret means restarting **every component that holds
it** — and different secret types have different restart-sets. A missing or stale
secret makes a service report `/readyz` **503 (retryable), not crash**, so a
half-rotated system looks like it's "still starting," not broken. That is the trap:
**an incomplete restart-set leaves stale credentials failing silently.**

## Restart-set per secret type
Rotate the secret, re-render it (`make rotate SERVICE=<svc>` then
`make agent-secrets` in dev; the Vault init-container in k8s), then restart:

| Secret | Restart-set | If you miss a component |
|---|---|---|
| **Agent token** (service `X`'s inbound token) | `X` **and** `outbox-worker` — the worker presents *X's* token to dispatch to it | worker keeps the old token → `401` dispatching to `X` → retries/dead-letter → **`OutboxBacklogHigh`** |
| **Gateway token — reason mode** | `llm-gateway` (its token→mode map) **and** `reasoner-agent` | reasoner's LLM calls `401` → **`LLMTemplateFallbackActive`** (`reason=rejected`) |
| **Gateway token — embed mode** (`gateway_token_embed`) | `llm-gateway` **and** `knowledge-service` | embedding/indexing calls `401` → retrieval silently degrades |
| **`postgres_dsn`** (DB credential) | **all 7 DB services**: `ingestion`, `watcher-agent`, `planner-agent`, `reasoner-agent`, `feedback-service`, `outbox-worker`, `knowledge-service` — **not** `llm-gateway` (it has no DB) | the missed service can't reach the DB → its `/readyz` 503, that pipeline stage stalls |
| **Webhook token** (`webhook_token_<source>`) | `ingestion` **and** reconfigure the **external** source (Prometheus Alertmanager / Kibana) that presents it | external side still sends the old token → `401` at ingestion → that source's alerts silently dropped |

## Verify
- Every restarted component reports `/readyz` 200 (`make ps-apps` in dev).
- Drive one test event end to end and confirm **no `401`s** in the outbox-worker
  dispatch logs or the gateway logs.
- Any alert the botched state would raise (`OutboxBacklogHigh`,
  `LLMTemplateFallbackActive`) stays clear for a few minutes.

## If recovery doesn't work / known limits / when to escalate
- **Silent-failure signature:** the rotation "looked done," then minutes later
  `OutboxBacklogHigh` or `LLMTemplateFallbackActive` fires. You missed a component
  in the restart-set — re-check the table for that secret type (the two most-missed
  are `outbox-worker` for agent tokens and the *external* source for webhook tokens).
- **Known limit — no hot-reload:** every rotation is a restart, so expect a brief
  `/readyz` 503 window. Kubernetes takes the pod out of rotation during it; delivery
  is at-least-once and retryable, so this costs latency, not data.
- **A service stays 503 after restart with the new secret present:** the secret file
  is probably malformed or empty — the config layer raises a `ConfigurationError`
  and `/readyz`'s `reason` names the offending file. If the Vault render itself is
  producing bad secrets, escalate to whoever owns the Vault templates.
