# radar-ingestion

The RADAR entry point. Detection happens outside RADAR (Prometheus alertmanager,
Kibana Watcher); ingestion receives those pre-fired alerts, normalizes them,
deduplicates them into incidents, and hands the pipeline its first outbox event.

**Ingestion is not an agent** (see [ADR 0011](../../docs/adr/0011-inbound-webhook-token.md)).
Inbound `/alerts/*` authenticate with a per-source `X-Radar-Webhook-Token` loaded
from Vault (never the internal `X-Radar-Agent-Token`), and there is no
`POST /events`: ingestion *produces* outbox events, it does not consume them.

## Endpoints

```
POST /alerts/prometheus   X-Radar-Webhook-Token   Prometheus alertmanager webhook
POST /alerts/kibana       X-Radar-Webhook-Token   Kibana Watcher webhook
POST /alerts/mock         X-Radar-Webhook-Token   mock/dev source
GET  /healthz                                     process liveness
GET  /readyz                                      DB reachable AND Vault secrets loaded
GET  /metrics                                     Prometheus text format
```

## Ingestion logic

```
1. Validate the per-source webhook token (Vault secret).
2. Normalize the vendor payload -> NormalizedAlert (source-specific normalizer).
3. fingerprint = sha256(service_name + ":" + alert_name + ":" + severity).
4. Look for an open incident with the same fingerprint within 5 minutes.
5. Found     -> attach the alert to that incident, write NO outbox event.
6. New       -> INSERT incident + INSERT alert + INSERT outbox_event
                (alert.normalized, target=watcher-agent), all in ONE transaction.
7. Respond 202 with the incident_id.
```

The 5-minute window is a boundary, not a rounding: an identical alert at 4m59s
attaches to the open incident; at 5m01s it opens a new one.

## One alert per request

Each POST carries exactly one alert and produces at most one incident. Prometheus
alertmanager batches alerts into a single webhook by default, so RADAR configures
alertmanager to **fan out** one alert per POST (a receiver whose `group_by` puts each
alert in its own group). A body that still arrives batched, one carrying an `alerts`
array, is rejected with **422**, never truncated to the first alert and never a crash
(see [ADR 0011](../../docs/adr/0011-inbound-webhook-token.md)). Any malformed or
incomplete payload is a 422 the same way.

Expected per-source bodies (one alert each):

```
prometheus  a single alertmanager alert object:
            {"status": "firing",
             "labels": {"alertname": "...", "service": "...", "severity": "..."},
             "annotations": {"summary": "..."},
             "startsAt": "2026-07-09T10:30:00Z", "fingerprint": "..."}

kibana      a Kibana Watcher webhook body:
            {"service_name": "...", "alert_name": "...", "severity": "...",
             "status": "firing", "triggered_at": "2026-07-09T10:30:00Z",
             "watch_id": "...", "labels": {...}, "annotations": {...}}

mock        a minimal test body (fired_at optional, defaults to now):
            {"service_name": "...", "alert_name": "...", "severity": "critical"}
```

`severity` is a **canonical, closed set**: `critical | high | medium | low | info`.
Every source must emit one of these; an unknown value (e.g. `warning`, `page`, `P1`)
is a **422**, never mapped or floored, so the same severity always compares equal
downstream (watcher escalation) and produces a stable dedup fingerprint.

## Webhook authentication

Every `/alerts/*` request must carry an `X-Radar-Webhook-Token` header, validated
**per source** ([ADR 0011](../../docs/adr/0011-inbound-webhook-token.md)): the
Prometheus endpoint accepts only the Prometheus token, and so on. A token valid
for one source presented to another's endpoint is rejected. A missing or wrong
token is **401**. Because auth is a trust boundary, 401 beats 422: a bad token
with a malformed body returns 401, not 422 (only authenticated callers get body
validation errors).

Each source's token lives in its **own** Vault secret file, so one can be rotated
or revoked without touching the others:

```
/vault/secrets/webhook_token_prometheus
/vault/secrets/webhook_token_kibana
/vault/secrets/webhook_token_mock
```

Each file holds just that source's `secrets.token_hex(32)` value. A source whose
file is absent is not loaded and its endpoint fails closed (401); at least one
must be present. In local dev, pull them from Vault into secret files with
`make ingestion-secrets` (writes the three files, matching the prod layout).

## Configuration and secrets

Non-secret settings come from `RADAR_*` environment variables. Secrets are read
from Vault-mounted files, never the environment
([ADR 0007](../../docs/adr/0007-vault-init-container.md)): the `postgres_dsn`
(the DSN embeds a password) and the per-source `webhook_token_*` files above.
`/readyz` is 200 only when both have loaded and the database is reachable.

## Run locally

```
uv run uvicorn radar_ingestion.main:app --port 8080

# requires a webhook_tokens secret; send the matching source token:
curl -sX POST localhost:8080/alerts/mock \
  -H "X-Radar-Webhook-Token: $MOCK_TOKEN" \
  -H 'content-type: application/json' \
  -d '{"service_name": "order-service", "alert_name": "OrderProcessingFailureRate", "severity": "critical"}'
```

## Docker

Build from the **repo root** (uv workspace resolves against the root lockfile):

```
docker buildx build --platform linux/amd64,linux/arm64 \
  -f apps/ingestion/Dockerfile -t radar-ingestion .
```
