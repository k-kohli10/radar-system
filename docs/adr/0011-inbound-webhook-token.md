# ADR 0011: Separate Webhook Token for External Inbound Alerts

## Status
Accepted

## Context
Ingestion is the one service that receives requests from outside RADAR's trust
boundary: Prometheus alertmanager and Kibana Watcher, both external systems posting to
`POST /alerts/prometheus` and `POST /alerts/kibana`. Every other authenticated endpoint
in RADAR uses the internal `X-Radar-Agent-Token`, one token per mode/service, issued
and rotated through Vault as part of the agent pipeline's trust model
(see the LLM Gateway's Token IAM). Reusing that same token scheme for external alert
sources would mean an alerting system credential and an internal agent credential share
the same shape and the same rotation path, even though they authenticate fundamentally
different trust levels. One is "another RADAR agent," the other is "an external
system we've configured to talk to us."

## Decision
External alert sources authenticate with a distinct header,
`X-Radar-Webhook-Token`, configured per source (Prometheus's token is not Kibana's
token), separate from the internal `X-Radar-Agent-Token` used between RADAR's own
services. Both are validated by the same request-validation discipline (missing or
wrong token → 401) but are issued, scoped, and rotated independently.

## Consequences
- Compromising or rotating an external source's credential never touches internal
  agent-to-agent auth, and vice versa. The two trust boundaries can be reasoned about
  and rotated independently.
- Adding a third external alert source later means minting one more webhook token, not
  reshaping the internal agent token scheme.
- Ingestion's route handlers must check the header name appropriate to the endpoint
  (`X-Radar-Webhook-Token` on `/alerts/*`, `X-Radar-Agent-Token` on `/events` if
  ingestion ever exposes one) rather than a single shared auth dependency. That's a
  small amount of duplication in exchange for a clear trust boundary.

## Inbound alert cardinality (one alert per request)

Ingestion normalizes exactly one alert per POST and opens exactly one incident per
new fingerprint — the pipeline's singular "202 with incident_id". Prometheus
alertmanager, however, batches multiple alerts into a single webhook body (an
`alerts` array) by default. RADAR therefore configures alertmanager to **fan out**
one alert per POST: a receiver whose grouping (`group_by`) places each alert in its
own group so each fires its own request.

A body that still arrives batched — one carrying an `alerts` array — is treated as a
misconfiguration and rejected with **422** (`InvalidPayloadError`), never silently
truncated to `alerts[0]` and never crashing the handler. The same 422 discipline
covers any malformed or incomplete payload. Kibana Watcher and the mock source send
one alert per request by construction.
