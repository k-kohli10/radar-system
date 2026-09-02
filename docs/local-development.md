# 🧑‍💻 Local Development

RADAR runs two ways. This guide covers **native** local dev: the backing services
in containers and the app processes on the host (`dev-apps-up`), for a fast edit
loop. To run everything in containers instead, see
[Running RADAR in Docker](operations/docker.md).

From a clean machine to a running stack in about ten minutes, most of it spent
pulling images. One script does the setup, and nothing secret touches the repo.

## Contents

- [🎯 TL;DR](#-tldr)
- [🧰 Prerequisites](#-prerequisites)
- [🚀 Step 1: Bootstrap](#-step-1-bootstrap)
- [🔑 Step 2: Add your external credentials](#-step-2-add-your-external-credentials)
- [🟢 Step 3: Start the stack](#-step-3-start-the-stack)
- [⚡ Everyday commands](#-everyday-commands)
- [🔐 Tokens and secrets](#-tokens-and-secrets)
- [🤖 Run the LLM gateway](#-run-the-llm-gateway)
- [🔥 Run the whole pipeline](#-run-the-whole-pipeline)
- [🪝 Git hooks](#-git-hooks)
- [🛟 Troubleshooting](#-troubleshooting)
- [🧭 Where to go next](#-where-to-go-next)

---

## 🎯 TL;DR

```bash
scripts/bootstrap.sh   # checks tools, installs uv, generates .env, syncs deps
make dev-infra-up               # start the whole stack
make dev-infra-ps                # watch all nine services come up
```

That's the happy path. The rest of this guide explains each step, what it does
under the hood, and how to dig yourself out when something goes sideways.

---

## 🧰 Prerequisites

Install these three yourself first. The bootstrap script **checks** for each one
and tells you precisely what's missing, but it won't install them for you
(they need a GUI or root, which a repo script has no business doing silently).

| Tool | Version | Check it |
|---|---|---|
| 🐍 **Python** | 3.12 or newer | `python3 --version` |
| 🐳 **Docker** | any recent | Docker Desktop (macOS) · Docker Engine (Linux) |
| 📦 **Docker Compose** | v2 plugin | `docker compose version` |

Everything else (`uv` and the entire Python dev toolchain) is installed for
you in the steps below.

---

## 🚀 Step 1: Bootstrap

From the repository root:

```bash
scripts/bootstrap.sh
```

Here's what happens, in order:

| # | Action | Detail |
|---|---|---|
| 1️⃣ | **Verify prerequisites** | Python ≥ 3.12, Docker, Compose v2; fails early with instructions if anything is missing |
| 2️⃣ | **Install `uv`** | via the official Astral installer into `~/.local/bin`, only if not already present |
| 3️⃣ | **Generate `.env`** | random per-machine secrets for Postgres, Vault, and Grafana; mode `600`, gitignored, never overwritten |
| 4️⃣ | **Run `make setup`** | `uv sync` builds `.venv/` from `uv.lock`, then `pre-commit install` wires up the git hooks |

> 💡 **Idempotent by design.** Re-run `scripts/bootstrap.sh` as often as you
> like. It never clobbers an existing `.env` and skips anything already done.

---

## 🔑 Step 2: Add your external credentials

Bootstrap generates every *internal* secret for you. Three *external* ones are
yours to supply. Open `.env` and replace the `__SET_ME__` placeholders:

```bash
OPENAI_API_KEY=...      # your OpenAI API key
SLACK_BOT_TOKEN=...     # xoxb-...  from your Slack app   (needed from Phase 9)
SLACK_APP_TOKEN=...     # xapp-...  for Socket Mode       (needed from Phase 9)
```

You don't need these to boot the local stack; only the phases that actually
call an LLM or Slack do.

> 🔒 **Never commit `.env` or paste its contents anywhere.** Every generated
> secret is unique to your machine. Lost it? Delete the file and re-run
> `scripts/bootstrap.sh` for a fresh set.

---

## 🟢 Step 3: Start the stack

```bash
make dev-infra-up
```

Nine containers come up via Docker Compose, plus a one-shot `es-traces-init` job
that installs the Elasticsearch traces index template and exits. Every port binds
to `127.0.0.1` **only**, so the dev stack is never exposed to your network.

| Service | Version | URL | Credentials |
|---|---|---|---|
| 🐘 **Postgres** | 16 | `localhost:5432` | user `radar`, password in `.env` |
| 🔎 **Elasticsearch** | 8.16.0 | http://localhost:9200 | none · security off, localhost only |
| 📊 **Kibana** | 8.16.0 | http://localhost:5601 | none |
| 🔥 **Prometheus** | v2.55.0 | http://localhost:9090 | none |
| 🚨 **Alertmanager** | v0.27.0 | http://localhost:9093 | none |
| 📈 **Grafana** | 11.3.0 | http://localhost:3000 | `admin`, password in `.env` |
| 🔐 **Vault** | 1.18.0 *(dev)* | http://localhost:8200 | root token in `.env` |
| 🔭 **OTel Collector** | contrib 0.119.0 | OTLP `localhost:4317` / `:4318` | none |
| 🪵 **Fluent Bit** | 3.2.2 | http://localhost:2020 | none |

> ⏳ **First run pulls ~3–4 GB of images.** Every start after that takes
> seconds.

### ✅ Verify everything is up

```bash
make dev-infra-ps
```

The seven services with health checks report `healthy` (Elasticsearch and Kibana
take 30–60s to get there); `otel-collector` and `fluent-bit` define no health check
and simply show `Up`. `es-traces-init` runs once and exits `0`. Prefer to spot-check
by hand?

```bash
curl -s http://localhost:9200/_cluster/health   # Elasticsearch
curl -s http://localhost:9090/-/healthy          # Prometheus
curl -s http://localhost:8200/v1/sys/health      # Vault
```

---

## ⚡ Everyday commands

Stack-wide:

| Command | What it does |
|---|---|
| `make dev-infra-up` · `make dev-infra-up` | start the container stack (detached) |
| `make dev-infra-stop` | stop the stack, **keep** data |
| `make clean` | stop the stack and **delete all data volumes** |
| `make dev-infra-ps` | status and health of all services |
| `make lint` | `ruff check` + `mypy` over the workspace |
| `make test` | run pytest |
| `make setup` | re-sync dependencies and reinstall hooks |

Single service: pass `s=<service>` where `<service>` is one of `postgres`,
`elasticsearch`, `kibana`, `prometheus`, `grafana`, `vault`:

| Command | What it does |
|---|---|
| `make start s=postgres` | start (or create) one service |
| `make stop-one s=kibana` | stop one service, leave the rest running |
| `make restart s=grafana` | restart one service |
| `make dev-infra-logs s=vault` | follow one service's logs (`make dev-infra-logs` for all) |

> 💾 **Your data is safe across restarts.** Postgres tables, Elasticsearch
> indices, and Grafana state survive `make dev-infra-stop` → `make dev-infra-up`. Only
> `make clean` wipes them.

> ⚠️ **Vault is the exception.** It runs `server -dev`, which is **in-memory**:
> every secret is gone the moment its container restarts: no volume, no
> `make clean` required. This is not a problem, it is the reason `make seed` and
> `make tokens` exist: they rebuild Vault from nothing. If a service suddenly
> reports `/readyz` 503 on a missing secret, an emptied Vault is the first thing
> to suspect. See [Tokens and secrets](#-tokens-and-secrets) below.

Database migrations (Alembic, against the compose Postgres):

| Command | What it does |
|---|---|
| `make migrate` | apply all pending migrations (`upgrade head`) |
| `make migrate-check` | verify models and migrations are in sync |
| `make migrate-down` | roll back the most recent migration |
| `make revision m="add foo table"` | autogenerate a new migration from model changes |

Tokens and secrets: see [Tokens and secrets](#-tokens-and-secrets) for what these
mean and when you need them:

| Command | What it does |
|---|---|
| `make seed` | write the human-supplied secrets (DSN, API key) from `.env` into Vault |
| `make tokens` | mint every platform token into Vault (idempotent: keeps what exists) |
| `make rotate SERVICE=reasoner-agent` | replace one service's tokens, and the worker's map entry for it |
| `make agent-secrets` | pull each agent's secrets into `~/.radar-dev/secrets/<service>/` |
| `make gateway-secrets` | pull the gateway's API key and token map |
| `make ingestion-secrets` | pull the per-source webhook tokens |
| `make dev-apps-up` | start the eight app processes |
| `make dev-apps-stop` | stop them |
| `make dev-apps-ps` | readiness table |
| `make dev-apps-logs` | tail all eight logs |
| `make index` | index `docs/runbooks/` into Elasticsearch (incremental) |

Docker (containerised stack, the alternative to running apps natively; full
guide in [`operations/docker.md`](operations/docker.md)). Run this **or** the
native `make dev-apps-up`, not both; they share host ports:

| Command | What it does |
|---|---|
| `make docker-up` | clean machine → running system: infra `--wait`, seed/tokens/migrate, then apps |
| `make docker-down` | tear down both stacks, **delete** volumes |
| `make docker-infra-up` | start the infra stack only (`--wait`) |
| `make docker-apps-up` | build + start the app stack (needs Vault already seeded) |
| `make docker-apps-restart` | re-run vault-init and restart apps to pick up re-seeded/rotated secrets |
| `make docker-apps-build` | build the app images without starting them |
| `make docker-apps-ps` · `make docker-apps-logs` | app stack status / follow logs |
| `make docker-apps-down` | stop apps, remove their secret volumes |
| `make docker-stop` · `make docker-start` | pause / resume both stacks, **keep** all data |
| `make docker-infra-stop` · `make docker-infra-start` | pause / resume the infra stack, keep data |
| `make docker-apps-stop` · `make docker-apps-start` | pause / resume the app stack, keep data |

---

## 🔐 Tokens and secrets

Services authenticate to each other with per-service tokens and read every secret
from a **file**, never an environment variable (ADR 0007). Locally, `make` plays the
part Kubernetes' init-container plays in the cluster. The token model itself (agent
vs gateway tokens, the per-service design) lives in
[ADR 0020](adr/0020-static-token-auth.md).

### The commands

```bash
make seed      # what a HUMAN supplies:   postgres_dsn, openai_api_key (from .env)
make tokens    # what the PLATFORM mints: agent tokens, dispatch map, gateway grants, webhook tokens
make agent-secrets      # pull -> ~/.radar-dev/secrets/<service>/
make gateway-secrets    # pull -> ~/.radar-dev/secrets/  (gateway + api key)
make ingestion-secrets  # pull -> ~/.radar-dev/secrets/  (webhook_token_*)
```

`seed`/`tokens` write to Vault; the `*-secrets` targets read it into files. Both
write-paths are safe to re-run: `seed` only copies what already exists in `.env`,
`tokens` only mints what is missing and never clobbers. `tokens` also rebuilds the
worker's `dispatch_tokens` from the per-service tokens each run, so the map stays in
step with them.

> ⚠️ **The dev Vault is in-memory** (`server -dev`): it empties on every container
> restart. Whenever it comes back empty, rebuild it with `make seed && make tokens`,
> then the three `*-secrets` pulls.

### Where the files land

One directory per service, mirroring its per-pod Vault mount in production:

```
~/.radar-dev/secrets/
├── watcher-agent/    agent_token, postgres_dsn
├── planner-agent/    agent_token, postgres_dsn
├── reasoner-agent/   agent_token, gateway_token, postgres_dsn
├── outbox-worker/    agent_token, dispatch_tokens, postgres_dsn
├── feedback-service/ agent_token, postgres_dsn, slack_bot_token, slack_app_token
├── openai_api_key         ┐
├── gateway_tokens         ├─ flat: the gateway and ingestion read these
└── webhook_token_*        ┘
```

Each service launches with `RADAR_SECRETS_DIR` at **its own** directory: two services
sharing one would share the `agent_token` file, collapsing per-service tokens into one.

### Rotating a service's tokens

```bash
make rotate SERVICE=reasoner-agent   # fresh agent + gateway token, and the worker's map entry
make agent-secrets                   # pull the new files
# restart reasoner-agent AND outbox-worker
```

> ⚠️ **Rotation is not hot.** Between the two restarts the worker still holds the old
> token; a dispatch then 401s, which is classified permanent and dead-lettered (not
> retried). Rotate on a drained pipeline: check outbox depth first.

Recover a dead-lettered event with the worker's admin endpoints:

```bash
TOK=$(cat ~/.radar-dev/secrets/outbox-worker/agent_token)
curl -s -H "X-Radar-Agent-Token: $TOK" localhost:8080/admin/dead-letters
curl -s -X POST -H "X-Radar-Agent-Token: $TOK" localhost:8080/admin/dead-letters/<event_id>/requeue
```

Rotating the Vault root token follows the same shape: put a new `VAULT_DEV_ROOT_TOKEN`
in `.env`, **recreate** the Vault container (`up -d --force-recreate vault`, not
`restart`, env binds at create time), then `make seed && make tokens` and the pulls.

> 🤫 These tools never print a secret value, only names and short prefixes. Do the
> same by hand: a value pasted into a terminal, chat, or PR is exposed even if
> deleted. Rotate rather than reason about it.

---

## 🤖 Run the LLM gateway

From Phase 4 onward you can run the LLM gateway as a real local server and hit
it with curl:

```bash
make gateway
```

| Command / variable | What it does |
|---|---|
| `make gateway` | start the gateway on http://localhost:8081 (Ctrl-C to stop) |
| `make gateway-secrets` | re-pull API keys + token map from Vault into the secrets dir (run after any Vault change, then restart the gateway) |
| `GATEWAY_PORT=9000 make gateway` | pick a different port |
| `GATEWAY_SECRETS_DIR=...` | where the secret files live (default `~/.radar-dev/secrets`) |
| `GATEWAY_CONFIG=...` | mode config path (default `apps/llm-gateway/config/gateway.yaml`) |

**Changing the backing model or provider** is a config edit, not a code
change: open [`apps/llm-gateway/config/gateway.yaml`](../apps/llm-gateway/config/gateway.yaml),
set `provider:` / `model:` for any mode (openai · anthropic · gemini), restart
the gateway, done. Two rules: the matching API key file must exist in the
secrets dir, and anthropic modes must set `max_output_tokens`.

The gateway follows the platform's secret rule even locally: it reads
**Vault-sourced files**, never environment variables. Two secret files must
exist in `GATEWAY_SECRETS_DIR` (the target checks and tells you which one is
missing):

| File | Contents | Vault path it mirrors |
|---|---|---|
| `openai_api_key` | your OpenAI API key, one line | `secret/radar/llm` |
| `gateway_tokens` | YAML map: `tokens: {<64-hex>: {service, allowed_mode}}` | `secret/radar/llm-gateway` |

Both are put into Vault by `make seed` (the API key, from `.env`) and `make tokens`
(the token map, minted), then pulled to files by `make gateway-secrets`: the same
flow the Kubernetes init-container performs (ADR 0007). Rotate with
`make rotate SERVICE=<service>`, re-pull, and restart the gateway.

Smoke-test it:

```bash
curl -s localhost:8081/readyz                    # {"status":"ready"}; 503 means a secret or config is missing

# Every gateway token grants exactly ONE mode, so the token you send decides the
# mode you may ask for. reasoner-agent is granted `extended`; asking it for any
# other mode is a 403, not a 401 — the token is valid, the mode is not.
TOKEN=$(cat ~/.radar-dev/secrets/reasoner-agent/gateway_token)
curl -s localhost:8081/v1/complete \
  -H "X-Radar-Agent-Token: $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"mode":"extended","messages":[{"role":"user","content":"hello"}]}'
```

> 🔑 **Tokens come from a service's own directory.** Each service reads its secrets
> from `~/.radar-dev/secrets/<service>/`, mirroring its per-pod Vault mount in
> production. There is no general-purpose "dev token" that can call anything: a
> token exists because a service needs it, and it can do exactly what that service
> is allowed to do.

> 📡 **Harmless noise:** `Transient error … exporting traces to localhost:4317`
> means the OTel Collector isn't running locally. It arrives with the
> observability phase; requests are unaffected.

---

## 🔥 Run the whole pipeline

Eight processes: ingestion, the three agents, the outbox worker, the
llm-gateway, the knowledge service, and the feedback-service. `make dev-apps-up`
starts them all, tracks PIDs in `.dev-run/`, and prints a readiness table.

### From nothing to a working pipeline

```bash
scripts/bootstrap.sh                # tools, uv, .env, deps
# edit .env: set OPENAI_API_KEY
#            (+ SLACK_BOT_TOKEN and SLACK_APP_TOKEN for feedback-service — Phase 9)

make dev-infra-up                      # 6 containers
make dev-infra-ps                             # wait: all healthy
make migrate                        # schema

make seed && make tokens            # .env -> Vault, then mint tokens
make agent-secrets
make gateway-secrets
make ingestion-secrets

make dev-apps-up                       # 8 processes
make index                          # build the runbook index
make dev-apps-ps                        # all 8 ready
```

`knowledge-service` reports **not ready** until `make index` has run: its
readiness check verifies the live index's vector dimension, and there is no
index before the first pass. It flips to ready on the next `make dev-apps-ps`.

`feedback-service` (Phase 9) needs **real** Slack tokens to go ready: it opens a
Socket Mode connection at startup (before it reports ready), so a placeholder
`SLACK_APP_TOKEN` passes secret-load but fails the connect and holds `/readyz` at
503. Without a real Slack app it stays not-ready and `recommendation.created`
retries; the rest of the pipeline (ingestion → agents → reasoner) is unaffected.

> ⚠️ `make dev-infra-stop` wipes the in-memory Vault (re-seed as above); Postgres
> and Elasticsearch use named volumes, so the index and past RCAs survive.

### Ports

| service | port | | service | port |
|---|---|---|---|---|
| llm-gateway | 8081 | | reasoner-agent | 8093 |
| ingestion | 8090 | | outbox-worker | 8094 |
| watcher-agent | 8091 | | knowledge-service | 8095 |
| planner-agent | 8092 | | feedback-service | 8096 |

### Complete e2e test

Test the full pipeline by firing alerts that exercise three scenarios: retrieval-grounded RCAs,
ungrounded RCAs (no runbook match), and LLM fallback (gateway down). The whole run takes about
a minute, most of it the two live LLM calls.

> ⚠️ **This posts to real Slack.** Completing the e2e drives the whole pipeline through
> feedback-service, which posts a real RCA card per scenario to whatever Slack workspace the
> stack is wired to (three cards). Run it against a personal or test channel, not a shared one.
> If feedback-service is not ready (no real Slack tokens), the pipeline still runs and stores
> the recommendations; only the Slack delivery step is skipped.

```bash
TOK=$(cat ~/.radar-dev/secrets/ingestion/webhook_token_mock)
fire() { curl -s -X POST http://127.0.0.1:8090/alerts/mock \
  -H "X-Radar-Webhook-Token: $TOK" -H "Content-Type: application/json" -d "$1"; echo; }

# how many recommendations exist now, so we can wait for ours to land
count() { docker exec radar-infra-postgres-1 psql -U radar -d radar -t \
  -c "SELECT count(*) FROM recommendations;" | tr -d ' '; }
base=$(count)

# Test 1: grounded. A runbook covers this alert; CRAG keeps ~5 chunks.
echo "=== Test 1: Grounded RCA (with runbook) ==="
fire '{"service_name":"order-service","alert_name":"OrderServiceHighMemory","severity":"medium"}'

# Test 2: ungrounded. Right service, but no runbook covers this alert.
echo "=== Test 2: Ungrounded RCA (no runbook) ==="
fire '{"service_name":"inventory-service","alert_name":"InventoryCustomerPiiExposedInLogs","severity":"high"}'

# WAIT for Tests 1 and 2 to commit BEFORE killing the gateway. Their LLM calls run
# asynchronously a few seconds after ingestion, so killing the gateway too early makes
# them fall back too, and all three come out as fallbacks instead of one.
echo "Waiting for Tests 1 and 2 to land..."
until [ "$(count)" -ge "$((base + 2))" ]; do sleep 2; done

# Test 3: fallback. The LLM gateway is down, so the reasoner templates the RCA.
echo "=== Test 3: Fallback RCA (LLM unavailable) ==="
kill $(cat .dev-run/llm-gateway.pid)
fire '{"service_name":"checkout-service","alert_name":"CheckoutTimeoutRate","severity":"high"}'
until [ "$(count)" -ge "$((base + 3))" ]; do sleep 2; done

# Test 3 killed the gateway; restart it so the stack is healthy again.
make dev-apps-up
```

A grounded or ungrounded alert takes a few seconds for the LLM; the fallback is near-instant,
since it makes no LLM call. Use a different service per scenario: a repeat within 5 minutes
deduplicates onto the open incident instead of creating a new one.

### Verify the results

After ~60 seconds (all three scenarios complete):

```sql
SELECT is_fallback, llm_provider, confidence,
       context_bundle->'retrieval'->>'outcome' AS retrieval,
       jsonb_array_length(context_bundle->'bundle'->'retrieved_context') AS chunks,
       left(root_cause, 80)
FROM recommendations ORDER BY created_at DESC LIMIT 3;
```

| scenario | is_fallback | retrieval | chunks | root_cause |
|---|---|---|---|---|
| grounded | `f` | `grounded` | 5 | cites the runbook's specifics |
| ungrounded | `f` | `grounded` | ~4 | states no runbook covers it (`confidence=low`) |
| gateway down | `t` | `unavailable` | 0 | `llm_provider=none`, fallback text |

The three `retrieval` outcomes are distinct and all recorded so an RCA's grounding
stays auditable: `grounded` (CRAG kept relevant chunks), `empty` (the grader judged
nothing relevant), and `unavailable` (retrieval failed outright, here because the
gateway was down).

Test 2 shows `grounded` with a few chunks rather than `empty`: the model rejects the
irrelevant chunks (`confidence=low`, "no runbook covers this") instead of retrieval
returning nothing. That is the pre-registered Phase 8 boundary-instability for
alert-shaped queries; see the limitation note in
[implementation_plan.md](implementation_plan.md).

Trace one incident end to end:

```sql
SELECT event_type, actor, created_at FROM audit_log
  WHERE correlation_id = (SELECT correlation_id FROM incidents ORDER BY opened_at DESC LIMIT 1)
  ORDER BY created_at;
```

`recommendation.created` is delivered by feedback-service (Phase 9): it posts the
RCA card to Slack and moves the incident to `investigating`, adding
`notification.delivered` and `incident.investigating` to the trail above. (It only
delivers when feedback-service is ready, i.e. real Slack tokens are set; otherwise
the event retries and dead-letters, correct then and still requeueable.)

---

## 🪝 Git hooks

`pre-commit` runs automatically on every commit, so bad things never make it
into history:

- 🔦 **gitleaks**: blocks any commit that smells like a secret
- 🎨 **ruff** (lint + format): auto-fixes style on the way in
- 🧹 **hygiene**: trailing whitespace, YAML/TOML syntax, oversized files,
  private keys, stray merge-conflict markers

The first commit after setup is slow while pre-commit builds its isolated hook
environments; every commit after that is fast. Sweep the whole tree on demand:

```bash
uv run pre-commit run --all-files
```

---

## 🛟 Troubleshooting

<details>
<summary><strong><code>make dev-infra-up</code> says ".env not found"</strong></summary>

Run `scripts/bootstrap.sh` first. Compose refuses to start without generated
credentials; there are no hardcoded defaults to fall back on.
</details>

<details>
<summary><strong>Elasticsearch is unhealthy or keeps exiting</strong></summary>

It needs ~1 GB free memory to itself. On Docker Desktop, bump the memory limit
(Settings → Resources) to at least **4 GB** for the whole stack.
</details>

<details>
<summary><strong>"Port already in use"</strong></summary>

Something already occupies 5432, 9200, 5601, 9090, 3000, or 8200. Stop the
other process, or edit the port mapping in
`deploy/compose/docker-compose-infra.yml` (change the **left** side of the mapping
only).
</details>

<details>
<summary><strong>I lost or corrupted my <code>.env</code></strong></summary>

```bash
rm .env && scripts/bootstrap.sh
make clean && make dev-infra-up
```

The `make clean` is **required**: Postgres, Grafana, and Vault cache the *old*
credentials inside their data volumes, so regenerating `.env` without wiping
volumes leaves them out of sync.
</details>

<details>
<summary><strong><code>uv: command not found</code> right after bootstrap</strong></summary>

The installer dropped `uv` into `~/.local/bin`, which may not be on the `PATH`
of already-open shells. Open a new terminal, or:

```bash
export PATH="$HOME/.local/bin:$PATH"
```
</details>

<details>
<summary><strong><code>feedback-service</code> stays DOWN or not-ready after <code>make dev-apps-up</code></strong></summary>

The feedback-service opens a real Socket Mode connection to Slack at startup and
does not report ready until it succeeds. This is deliberate: a bot that cannot
hear button clicks and mentions is broken from the start, and discovering that
when an incident card is already in the channel is worse than refusing to go
ready.

**Check your Slack tokens are valid:**

1. Verify `SLACK_BOT_TOKEN` and `SLACK_APP_TOKEN` in `.env` are real (not
   placeholders like `__SET_ME__`). They must be from an actual Slack workspace
   where the RADAR bot is installed.
2. Confirm the bot has permissions: **Socket Mode** enabled at the app level, and
   the bot user scope includes `app_mentions:read` and `reactions:write`.
3. Check the logs: `tail .dev-run/feedback-service.log` for the actual error.
   Common issues are revoked tokens, missing app-level Socket Mode permissions,
   or network timeouts.

**If tokens are valid but the connection hangs**, it may be a network issue or
the Slack SDK waiting on a timeout. Kill it and try again:

```bash
pkill -f "feedback-service"
make dev-apps-up  # restarts it
```

**If you have no real Slack workspace**, the rest of the pipeline works fine
without it: ingestion → watcher → planner → reasoner all run and produce
recommendations. Only the delivery step (posting RCA cards) is blocked. The
`recommendation.created` event retries in the outbox while feedback-service is
not ready, and succeeds once you plug in real credentials or until it
dead-letters (see "Recovering a dead-lettered event" above).
</details>

---

## 🧭 Where to go next

| Path | What's there |
|---|---|
| [`../README.md`](../README.md) | What RADAR is, the problem it solves, and how it works |
| [`roadmap.md`](roadmap.md) | What's shipped, what's next |
| [`architecture/`](architecture/) | System overview, agent pipeline, data model, sequence flows |
| [`operations/docker.md`](operations/docker.md) | Running the whole stack in Docker + the end-to-end test |
| [`implementation_plan.md`](implementation_plan.md) | The full technical specification |
