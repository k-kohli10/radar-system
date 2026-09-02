# 🚨 Runbook: LLM gateway failure

**Alert:** `LLMTemplateFallbackActive` (warning), plus `RadarAgentDown{service="llm-gateway"}` (critical) if the gateway is fully down.
**Dashboards:** `llm-gateway`, `incident-pipeline`.

## Contents

- 🔎 [Symptom](#symptom)
- 💥 [Impact (read this first)](#impact-read-this-first)
- 🩺 [Diagnose](#diagnose)
- 🛠️ [Recover](#recover)
- 🆘 [If recovery doesn't work / known limits / when to escalate](#if-recovery-doesnt-work--known-limits--when-to-escalate)

## Symptom
`radar_recommendations_fallback_total` is climbing and the reasoner is producing
**template RCAs** instead of model analyses (`is_fallback=true`, `confidence=low`,
`model_id=template-fallback`). The alert carries the `reason` label.

## Impact (read this first)
This is **graceful degradation, not an outage.** Every incident still gets an RCA:
the reasoner falls back to a checklist built from the plan's own investigation
steps. It is worse than a model analysis and strictly better than nothing: no
incident is dropped. So the goal is to restore the LLM path calmly, not to treat
this as a fire.

## Diagnose
1. **Is the gateway up?** `curl -s localhost:8081/readyz`. If `RadarAgentDown{service=llm-gateway}` is also firing, the process is down or unreachable.
2. **What is the fallback `reason`?** From the alert label / `radar_recommendations_fallback_total{reason=...}`:
   - `gateway_unavailable`: the reasoner can't reach the gateway (process down, wrong `RADAR_GATEWAY_URL`, network).
   - `rejected`: the gateway returned 401 because the reasoner's gateway token no longer matches. See [vault-secret-rotation](vault-secret-rotation.md) (gateway-token restart-set).
   - `not_json` / `schema_invalid`: the gateway answered but the model output failed validation (a model/config problem, not infra).
3. **Is the provider erroring?** Check `radar_llm_provider_errors_total` on the `llm-gateway` dashboard and the OpenAI status page.

## Recover
- **Process down** (`gateway_unavailable`, gateway `/readyz` not 200): restart it. `make dev-apps-up` restarts stopped host apps; in k8s the pod restarts itself. Confirm `/readyz` 200; watch the fallback rate return to 0.
- **401 (`rejected`):** the gateway token is stale. Follow [vault-secret-rotation](vault-secret-rotation.md), restarting **both** `llm-gateway` and `reasoner-agent`.

## If recovery doesn't work / known limits / when to escalate
- **During an OpenAI (provider) outage, do NOT thrash the gateway.** Restarting a
  healthy gateway does nothing for a provider-side outage, and a restart loop just
  adds churn. The template fallback **is** the designed mitigation: every incident
  keeps getting a usable RCA. Confirm it's provider-side via the OpenAI status page
  and rising `radar_llm_provider_errors_total`, then **wait**: the fallback rate
  clears itself when the provider recovers. Escalate to whoever owns the provider
  account (quota, rate limits, billing) instead of restarting RADAR.
- **Circuit breaker (expected, not a fault).** After a provider binding fails
  repeatedly, the gateway opens its circuit and fails that binding fast, skipping the
  retry backoff, so requests fall to the fallback provider (or the template RCA)
  without each one waiting out the full retry budget. `radar_llm_circuit_breaker_state{provider,model}`
  reads 1 (open) or 2 (half-open) while this is happening; it returns to 0 (closed) on
  its own once a trial call to the recovered provider succeeds. An open circuit during a
  known provider outage is the breaker doing its job, not a separate incident to chase.
- **Known limit:** fallback RCAs are `confidence=low` / `is_fallback=true`. Treat
  them as leads, not conclusions, until the LLM path is back.
- **Gateway up, token valid, provider healthy, but fallbacks persist**
  (`not_json` / `schema_invalid`): the model's output is failing validation. This is a
  model or prompt/config problem, not something ops can restart away. Escalate to
  the RADAR maintainer.
