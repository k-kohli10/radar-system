# 🚪 radar-llm-gateway

The single point of LLM access for RADAR. No other service talks to an LLM
provider directly.

Agents call the gateway with a static agent token (`X-Radar-Agent-Token`) that
maps to exactly one mode (`fast`, `reason`, `extended`, `embed`). Each mode is
configured with a provider, model, input/output token limits, and a timeout.
Provider failures are retried with backoff (1s, 3s, 9s), then routed to a
fallback provider if one is configured; if that also fails the gateway returns
503 and the caller degrades (the Reasoner falls back to a template RCA).

## 📚 Contents

- [🔗 Endpoints](#-endpoints)
- [🔀 Request validation order](#-request-validation-order)
- [🙈 Logging policy](#-logging-policy)
- [☁️ Providers](#-providers)
- [▶️ Run locally](#-run-locally)

## 🔗 Endpoints

```
POST /v1/complete   chat completion (streaming via "stream": true)
POST /v1/embed      embeddings
GET  /healthz       process liveness
GET  /readyz        config and provider credentials loaded
GET  /metrics       Prometheus text format
```

## 🔀 Request validation order

```
1. Extract X-Radar-Agent-Token header
2. Token not in config           -> 401
3. Extract mode from body
4. mode != token's allowed_mode  -> 403
5. Input tokens over mode limit  -> 422
6. Route to provider, enforce per-mode timeout
7. On failure: retry 3x (1s/3s/9s), then fallback provider
8. Fallback also fails           -> 503
```

## 🙈 Logging policy

Never logged: message content, API keys, agent tokens, raw LLM response bodies.
Logged: mode, provider, model, prompt_tokens, completion_tokens, latency_ms,
status_code.

## ☁️ Providers

OpenAI, Anthropic, and Gemini via their individual SDKs (`openai`,
`anthropic`, `google-generativeai`). No LangChain, LangGraph, or LiteLLM.
Provider/model per mode is config, not code: edit
[`config/gateway.yaml`](config/gateway.yaml) and restart. Adapters live in
`plugins/llm/`.

## ▶️ Run locally

```
uv run uvicorn radar_llm_gateway.main:app --port 8081
```
