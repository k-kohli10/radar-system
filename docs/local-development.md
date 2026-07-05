# 🧑‍💻 Local Development

From a clean laptop to six running services in about **ten minutes** — most of
that spent pulling Docker images while you grab a coffee. One script does the
setup, one command starts the stack, and nothing secret ever touches the repo.

---

## 🎯 TL;DR

```bash
scripts/bootstrap.sh   # checks tools, installs uv, generates .env, syncs deps
make dev               # start the whole stack
make ps                # watch all six services go healthy
```

That's the happy path. The rest of this guide explains each step, what it does
under the hood, and how to dig yourself out when something goes sideways.

---

## 🧰 Prerequisites

Install these three yourself first. The bootstrap script **checks** for each one
and tells you precisely what's missing — but it won't install them for you
(they need a GUI or root, which a repo script has no business doing silently).

| Tool | Version | Check it |
|---|---|---|
| 🐍 **Python** | 3.12 or newer | `python3 --version` |
| 🐳 **Docker** | any recent | Docker Desktop (macOS) · Docker Engine (Linux) |
| 📦 **Docker Compose** | v2 plugin | `docker compose version` |

Everything else — `uv`, and the entire Python dev toolchain — is installed for
you in the steps below.

---

## 🚀 Step 1 — Bootstrap

From the repository root:

```bash
scripts/bootstrap.sh
```

Here's what happens, in order:

| # | Action | Detail |
|---|---|---|
| 1️⃣ | **Verify prerequisites** | Python ≥ 3.12, Docker, Compose v2 — fails early with instructions if anything is missing |
| 2️⃣ | **Install `uv`** | via the official Astral installer into `~/.local/bin`, only if not already present |
| 3️⃣ | **Generate `.env`** | random per-machine secrets for Postgres, Vault, and Grafana — mode `600`, gitignored, never overwritten |
| 4️⃣ | **Run `make setup`** | `uv sync` builds `.venv/` from `uv.lock`, then `pre-commit install` wires up the git hooks |

> 💡 **Idempotent by design.** Re-run `scripts/bootstrap.sh` as often as you
> like — it never clobbers an existing `.env` and skips anything already done.

---

## 🔑 Step 2 — Add your external credentials

Bootstrap generates every *internal* secret for you. Three *external* ones are
yours to supply — open `.env` and replace the `__SET_ME__` placeholders:

```bash
OPENAI_API_KEY=...      # your OpenAI API key
SLACK_BOT_TOKEN=...     # xoxb-...  from your Slack app   (needed from Phase 9)
SLACK_APP_TOKEN=...     # xapp-...  for Socket Mode       (needed from Phase 9)
```

You don't need these to boot the local stack — only the phases that actually
call an LLM or Slack do.

> 🔒 **Never commit `.env` or paste its contents anywhere.** Every generated
> secret is unique to your machine. Lost it? Delete the file and re-run
> `scripts/bootstrap.sh` for a fresh set.

---

## 🟢 Step 3 — Start the stack

```bash
make dev
```

Six containers come up via Docker Compose. Every port binds to `127.0.0.1`
**only** — the dev stack is never exposed to your network.

| Service | Version | URL | Credentials |
|---|---|---|---|
| 🐘 **Postgres** | 16 | `localhost:5432` | user `radar`, password in `.env` |
| 🔎 **Elasticsearch** | 8.16.0 | http://localhost:9200 | none · security off, localhost only |
| 📊 **Kibana** | 8.16.0 | http://localhost:5601 | none |
| 🔥 **Prometheus** | v2.55.0 | http://localhost:9090 | none |
| 📈 **Grafana** | 11.3.0 | http://localhost:3000 | `admin`, password in `.env` |
| 🔐 **Vault** | 1.18.0 *(dev)* | http://localhost:8200 | root token in `.env` |

> ⏳ **First run pulls ~3–4 GB of images.** Every start after that takes
> seconds.

### ✅ Verify everything is up

```bash
make ps
```

All six should report `healthy` (Elasticsearch and Kibana take 30–60s to get
there). Prefer to spot-check by hand?

```bash
curl -s http://localhost:9200/_cluster/health   # Elasticsearch
curl -s http://localhost:9090/-/healthy          # Prometheus
curl -s http://localhost:8200/v1/sys/health      # Vault
```

---

## 🎛️ Everyday commands

Stack-wide:

| Command | What it does |
|---|---|
| `make dev` | start the local stack (detached) |
| `make stop` | stop the stack, **keep** data |
| `make clean` | stop the stack and **delete all data volumes** |
| `make ps` | status and health of all services |
| `make lint` | `ruff check` + `mypy` over the workspace |
| `make test` | run pytest |
| `make setup` | re-sync dependencies and reinstall hooks |

Single service — pass `s=<service>` where `<service>` is one of `postgres`,
`elasticsearch`, `kibana`, `prometheus`, `grafana`, `vault`:

| Command | What it does |
|---|---|
| `make start s=postgres` | start (or create) one service |
| `make stop-one s=kibana` | stop one service, leave the rest running |
| `make restart s=grafana` | restart one service |
| `make logs s=vault` | follow one service's logs — `make logs` for all |

> 💾 **Your data is safe across restarts.** Postgres tables, Elasticsearch
> indices, and Grafana state survive `make stop` → `make dev`. Only
> `make clean` wipes them.

---

## 🪝 Git hooks

`pre-commit` runs automatically on every commit, so bad things never make it
into history:

- 🔦 **gitleaks** — blocks any commit that smells like a secret
- 🎨 **ruff** (lint + format) — auto-fixes style on the way in
- 🧹 **hygiene** — trailing whitespace, YAML/TOML syntax, oversized files,
  private keys, stray merge-conflict markers

The first commit after setup is slow while pre-commit builds its isolated hook
environments; every commit after that is fast. Sweep the whole tree on demand:

```bash
uv run pre-commit run --all-files
```

---

## 🛟 Troubleshooting

<details>
<summary><strong><code>make dev</code> says ".env not found"</strong></summary>

Run `scripts/bootstrap.sh` first. Compose refuses to start without generated
credentials — there are no hardcoded defaults to fall back on.
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
`deploy/compose/docker-compose.yml` (change the **left** side of the mapping
only).
</details>

<details>
<summary><strong>I lost or corrupted my <code>.env</code></strong></summary>

```bash
rm .env && scripts/bootstrap.sh
make clean && make dev
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

---

## 🧭 Where to go next

| Path | What's there |
|---|---|
| [`../README.md`](../README.md) | What RADAR is, the problem it solves, and how it works |
| [`roadmap.md`](roadmap.md) | The phase-by-phase build plan |
| [`architecture/`](architecture/) | System overview, agent pipeline, data model, sequence flows |
| [`implementation_plan.md`](implementation_plan.md) | The full technical specification |
