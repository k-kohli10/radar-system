# ⏱️ 15-Minute Quickstart

Go from a fresh clone to a live, LLM-generated root cause analysis on your own
machine. Everything runs locally in Docker: no cloud, no Kubernetes.

**Total time:** ~15 minutes, most of it the one-time image build.

---

## Prerequisites

| Need | Why | Check |
|---|---|---|
| **Docker** (Desktop or Engine) + Compose v2, running | Runs the whole stack | `docker compose version` |
| **Python** ≥ 3.12 | `bootstrap.sh` uses it to generate credentials | `python3 --version` |
| **git**, **curl** | Clone + drive the demo | n/a |
| **An OpenAI API key** | The LLM gateway won't start without it | [platform.openai.com](https://platform.openai.com/api-keys) |
| *(optional)* **A Slack app** (bot + app token) | To see the RCA delivered as a Slack card | n/a |

`uv` is installed automatically by the bootstrap script if it's missing.
Slack is optional: without it, the RCA still lands in Postgres and you verify it there.

---

## 1. Clone and bootstrap · ~2 min

```bash
git clone https://github.com/k-kohli10/radar-system.git
cd radar-system
scripts/bootstrap.sh
```

`bootstrap.sh` checks your tools, installs `uv` if needed, and generates a
gitignored `.env` with random per-machine credentials. It never overwrites an
existing `.env`, and never prints a secret.

## 2. Add your OpenAI key · ~1 min

Open `.env` and set your key (this one is **required**):

```bash
OPENAI_API_KEY=sk-...
```

To get the Slack card too, also set `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN`.
Skip them for now if you just want to see the RCA in the database.

## 3. Bring up the stack · ~6–8 min (first build)

```bash
make docker-up          # infra (--wait) → seed + tokens + migrate → apps (build + up)
make docker-apps-ps     # all app containers should read "Up"
```

`make docker-up` starts the infra (Postgres, Vault, Elasticsearch, Prometheus,
Grafana), seeds Vault and runs migrations on the host, then builds and starts the
eight services plus a platform-sim demo target. The first run builds images, so
this is the slow step; later runs are fast.

Confirm the gateway is live:

```bash
curl -s localhost:8081/readyz; echo    # llm-gateway → ready
```

## 4. Index the runbooks · ~1–2 min

`knowledge-service` returns `503` until the runbook corpus is embedded into
Elasticsearch: this is what lets RADAR ground its RCAs in real runbooks.

```bash
make agent-secrets && make index       # embed runbooks into Elasticsearch
curl -s localhost:8095/readyz; echo    # knowledge-service → ready
```

## 5. Fire an alert · ~1 min

Post an alert straight to ingestion with its webhook token. The richer the
`labels`/`annotations`, the sharper the RCA; this one carries a deploy id and a
dominant error class, so expect **high confidence**:

```bash
TOK=$(docker exec radar-apps-ingestion-1 cat /vault/secrets/webhook_token_mock)
curl -s -X POST http://127.0.0.1:8090/alerts/mock \
  -H "X-Radar-Webhook-Token: $TOK" -H "Content-Type: application/json" \
  -d '{
    "service_name": "order-service",
    "alert_name": "OrderProcessingFailureRate",
    "severity": "critical",
    "labels": {"service": "order-service", "deployment": "order-service", "error_class": "SQLSTATE_23505", "env": "prod"},
    "annotations": {"summary": "Order failure rate 42% (threshold 5%)", "description": "Began 4 min after deploy order-service@v2.8.1 (deploy id d-9f2a1). 91% of failures are Postgres UniqueViolation SQLSTATE 23505 on orders_pkey. payment-gateway and inventory-service healthy."}
  }'; echo
```

> On repeat runs within 5 minutes, vary the `alert_name` to open a fresh incident:
> the dedup fingerprint is `service_name:alert_name:severity`, so a new `alert_name`
> is enough. Keep `service_name` as `order-service` so runbook retrieval still
> grounds: retrieval filters by service, so a different `service_name` yields
> `retrieval = empty` and RADAR reasons from the alert alone (still a real,
> high-confidence RCA, just runbook-free).

## 6. See the result · ~1 min

Watch the reasoner produce the RCA:

```bash
docker compose -p radar-apps logs -f reasoner-agent    # Ctrl+C when the RCA appears
```

Then confirm it persisted (this works whether or not Slack is configured):

```bash
docker exec radar-infra-postgres-1 psql -U radar -d radar -c \
  "select confidence, is_fallback, \
          context_bundle->'retrieval'->>'outcome' as retrieval, left(root_cause,60) \
   from recommendations order by created_at desc limit 1;"
```

You should see `confidence = high`, `is_fallback = f` (a real LLM answer, not the
template fallback), and `retrieval = grounded` (a runbook matched the incident).
If you set the Slack tokens, the same RCA arrives as a card in your Slack channel,
with 👍 / 👎 / ✅ Resolve buttons.

---

## What just happened

```
your alert → ingestion → watcher → planner → reasoner → RCA
                (Postgres transactional outbox at every hop)
                                 │
                    knowledge-service (graded runbook retrieval)
                                 │
                          Slack card + feedback
```

RADAR normalized and deduplicated the alert, correlated it into an incident, built
an investigation plan, retrieved and graded the relevant runbook, called the LLM to
produce a grounded RCA with recommended actions, and delivered it: all coordinated
through a Postgres outbox, no direct calls between agents.

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `llm-gateway` won't start | `OPENAI_API_KEY` missing in `.env` | Set it, then `make docker-apps-restart` |
| `knowledge-service` stays 503 | Runbooks not indexed | Run `make agent-secrets && make index` |
| `is_fallback = t` | Gateway couldn't reach the LLM | Check the key and `curl localhost:8081/readyz` |
| Alert didn't open an incident | Duplicate fingerprint within 5 min | Change `service_name` and re-fire |

## Next steps

- **Deeper Docker walkthrough** (ports, observability, commands): [operations/docker.md](operations/docker.md)
- **Run on Kubernetes**: [operations/kubernetes-cd.md](operations/kubernetes-cd.md)
- **How it works**: [architecture/agent-pipeline.md](architecture/agent-pipeline.md)
- **Tear it all down**: `make docker-down`
