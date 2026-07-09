# radar-ingestion

The RADAR entry point. Detection happens outside RADAR (Prometheus alertmanager,
Kibana Watcher); ingestion receives those pre-fired alerts, normalizes them,
deduplicates them into incidents, and hands the pipeline its first outbox event.

**Ingestion is not an agent** (see [ADR 0011](../../docs/adr/0011-inbound-webhook-token.md)).
Inbound `/alerts/*` authenticate with a per-source `X-Radar-Webhook-Token` loaded
from Vault — never the internal `X-Radar-Agent-Token` — and there is no
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

## Configuration and secrets

Non-secret settings come from `RADAR_*` environment variables. The Postgres DSN
embeds a password, so it is a **secret**: it is read from the Vault-mounted
`postgres_dsn` file, never from the environment
([ADR 0007](../../docs/adr/0007-vault-init-container.md)). The per-source webhook
tokens are likewise loaded from Vault.

## Run locally

```
uv run uvicorn radar_ingestion.main:app --port 8080
```

## Docker

Build from the **repo root** (uv workspace resolves against the root lockfile):

```
docker buildx build --platform linux/amd64,linux/arm64 \
  -f apps/ingestion/Dockerfile -t radar-ingestion .
```
