# 🚪 ADR 0004: A Single LLM Gateway for All Provider Calls

## Contents

- [Status](#-status)
- [Context](#-context)
- [Decision](#-decision)
- [Consequences](#-consequences)
- [Amendment (Phase 4): where the config and the token map live](#-amendment-phase-4-where-the-config-and-the-token-map-live)

## 🚦 Status
Accepted

## 🧩 Context
Only reasoner-agent (RCA generation) and knowledge-service (embeddings, CRAG grading)
need LLM calls, but every such call needs the same things: per-mode model selection,
timeout enforcement, retries, a fallback provider if the primary fails, token/latency
metrics, and a guarantee that prompt content and API keys never end up in logs. Letting
each service hold its own provider API key and implement its own retry/fallback logic
means that behavior, and the security surface of who holds a raw OpenAI/Anthropic key,
gets duplicated and drifts service by service.

## ✅ Decision
One internal service, `llm-gateway`, is the only thing in RADAR that holds real
provider API keys. Every other service authenticates to it with a static per-agent
token (`X-Radar-Agent-Token`, `secrets.token_hex(32)`, Vault-stored) scoped to exactly
one mode (`fast | reason | extended | embed`). The gateway owns:

```
1. Token IAM: token → allowed mode, enforced per request
2. Per-mode config: provider, model, token limits, timeout
3. Request validation: token → mode match → input token count → provider call
4. Retry: 3 attempts, 1s/3s/9s backoff, on 429/500/502/503/504/timeout
5. Fallback: if primary provider fails after retries, try the configured fallback
   provider before giving up
6. 503 to the caller only if both primary and fallback are exhausted
```

Provider adapters (`plugins/llm/{openai,anthropic,gemini}/`) implement a common
`LLMProvider` protocol from `packages/contracts`. Swapping providers is a config
change (see the LLM Provider Strategy in the implementation plan), never a code change.
Default provider for all four modes is OpenAI; Anthropic/Gemini are available via
config swap for anyone else who clones the repo.

When the gateway itself can't produce an answer (503), the caller doesn't get to just
fail: reasoner-agent falls back to a template-based RCA built from the investigation
plan's steps, marked `is_fallback=true`, `confidence=low`. An incident is never left
without a recommendation, even during a full provider outage.

## ⚖️ Consequences
- Exactly one place in the codebase touches `anthropic`/`openai`/`google-generativeai`
  SDKs directly: the plugin adapters behind the gateway.
- The gateway is a small, purpose-built router: it routes a request to a provider,
  enforces the per-mode config, and logs the result. See [ADR 0019](0019-no-llm-frameworks.md)
  for why it uses no orchestration framework.
- Losing an OpenAI key does not mean losing the gateway itself for other reasons. A
  provider-level outage degrades gracefully to a fallback provider, then to a template
  answer, never a hard failure that leaves an engineer with nothing.
- Rate limits, spend, and latency are visible in one place
  (`radar_llm_*` metrics), not scattered across every service that happens to call an
  LLM.
- The gateway is a single point of failure for all LLM-dependent behavior in RADAR.
  Phase 13 adds a circuit breaker to fail fast against a provider that is currently
  down, rather than retrying into a known-bad backend on every request.

## 🛠️ Amendment (Phase 4): where the config and the token map live

The plan's gateway config shape shows `modes`, `fallback`, and `tokens` as one
document, but token values are secrets and the mode table is meant to be a
ConfigMap. Implementation resolved the conflict by splitting the two along the
platform's config/secret boundary (ADR 0007):

- **Mode routing** (`modes` + `fallback`) is non-secret YAML, checked in at
  `apps/llm-gateway/config/gateway.yaml` and mounted as a ConfigMap in
  production. Path override: `RADAR_GATEWAY_CONFIG_PATH`. Changing a mode's
  provider/model is an edit to this file plus a restart, never a code change.
- **The token→mode map** lives in a single Vault secret, `gateway_tokens`
  (path `secret/radar/llm-gateway`), holding a YAML map of
  `token → {service, allowed_mode}`. The init-container writes it to
  `/vault/secrets/gateway_tokens` like any other secret file; it never appears
  in the ConfigMap or the environment.
- **Provider API keys** are separate Vault-sourced files
  (`openai_api_key`, `anthropic_api_key`, `gemini_api_key` at
  `secret/radar/llm`), read at startup only for providers the config actually
  references.

Both config and token map are loaded once at startup, so rotation follows the
platform rule: change in Vault, restart the pod. Because `gateway_tokens` is a
map, rotation can be zero-downtime: add the new token alongside the old,
restart, move the caller, remove the old token, restart.

The gateway is also the one service bootstrapped with `with_agent_auth=False`:
it has no single inbound `agent_token` of its own and instead enforces caller
auth itself against the token map (constant-time comparison, tokens never
logged or echoed in errors).
