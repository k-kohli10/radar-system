# 🪝 ADR 0011: Separate Webhook Token for External Inbound Alerts

## Contents

- [Status](#-status)
- [Context](#-context)
- [Decision](#-decision)
- [Consequences](#-consequences)
- [Comparison](#-comparison)
- [Inbound alert cardinality (one alert per request)](#-inbound-alert-cardinality-one-alert-per-request)
- [Token storage: one file per source](#-token-storage-one-file-per-source)
- [Known limitation: readyz vs partial token provisioning](#-known-limitation-readyz-vs-partial-token-provisioning)

## 🚦 Status
Accepted

## 🧩 Context
Ingestion is the one service that receives requests from outside RADAR's trust
boundary: Prometheus alertmanager and Kibana Watcher, both external systems posting to
`POST /alerts/prometheus` and `POST /alerts/kibana`. Every other authenticated endpoint
in RADAR uses the internal `X-Radar-Agent-Token`, one token per mode/service, issued
and rotated through Vault as part of the agent pipeline's trust model (see the LLM
Gateway's Token IAM).

## ✅ Decision
External alert sources authenticate with a distinct header,
`X-Radar-Webhook-Token`, configured per source (Prometheus's token is not Kibana's
token), separate from the internal `X-Radar-Agent-Token` used between RADAR's own
services. Both are validated by the same request-validation discipline (missing or
wrong token → 401) but are issued, scoped, and rotated independently.

## ⚖️ Consequences
- Compromising or rotating an external source's credential never touches internal
  agent-to-agent auth, and vice versa. The two trust boundaries can be reasoned about
  and rotated independently.
- Adding a third external alert source later means minting one more webhook token, not
  reshaping the internal agent token scheme.
- Ingestion's route handlers must check the header name appropriate to the endpoint
  (`X-Radar-Webhook-Token` on `/alerts/*`, `X-Radar-Agent-Token` on `/events` if
  ingestion ever exposes one) rather than a single shared auth dependency. That's a
  small amount of duplication in exchange for a clear trust boundary.

## 🆚 Comparison

| Alternative | What it's for | Why RADAR skips it |
|---|---|---|
| Reuse `X-Radar-Agent-Token` for external sources | One token scheme for every authenticated caller | Conflates two different trust levels, an internal RADAR agent versus an external system RADAR has configured to talk to it, under the same shape and rotation path |

## 🔢 Inbound alert cardinality (one alert per request)

Ingestion normalizes exactly one alert per POST and opens exactly one incident per
new fingerprint (the pipeline's singular "202 with incident_id"). Prometheus
alertmanager, however, batches multiple alerts into a single webhook body (an
`alerts` array) by default. RADAR therefore configures alertmanager to **fan out**
one alert per POST: a receiver whose grouping (`group_by`) places each alert in its
own group so each fires its own request.

A body that still arrives batched, one carrying an `alerts` array, is treated as a
misconfiguration and rejected with **422** (`InvalidPayloadError`), never silently
truncated to `alerts[0]` and never crashing the handler. The same 422 discipline
covers any malformed or incomplete payload. Kibana Watcher and the mock source send
one alert per request by construction.

## 🗄️ Token storage: one file per source

Each source's webhook token is a **separate** Vault secret file
(`webhook_token_prometheus`, `webhook_token_kibana`, `webhook_token_mock`), not a
single combined map. This is deliberate: rotating or revoking one source's token
rewrites only that source's file and never touches the others. Ingestion assembles
them into an in-memory per-source map at startup; a source whose file is absent is
not loaded and its endpoint fails closed (401).

## ⚠️ Known limitation: readyz vs partial token provisioning

`/readyz` currently reports ready when **at least one** webhook token loaded. A source
whose token file is missing fails closed (its endpoint 401s every alert) while readyz
stays 200. The service can report healthy while silently dropping one source's alerts,
a poor failure mode for an incident platform.

The correct behavior is for readyz to 503 when any **configured active source** is
missing its token: dev declares `sources=[mock]`, prod declares
`sources=[prometheus, kibana]` and fails readiness if either token did not mount.
That requires an explicit per-deployment "active sources" configuration (there is no
safe universal default: requiring all sources breaks single-source dev, requiring
none is today's behavior). Introducing that config is deferred to the Phase 13
security and resilience audit; it is recorded here as a known limitation rather than
a silent gap. Until then, deployments must ensure every active source's token file is
mounted.
