# 🤝 Contributing to RADAR

RADAR follows a locked architecture, recorded in
[docs/implementation_plan.md](docs/implementation_plan.md) and the
[ADRs](docs/adr/): the source of truth for scope and design decisions. 📖 **Read
the relevant ADR before opening a PR that touches it.**

---

## 📚 Contents

- [🧭 Ground Rules](#-ground-rules)
- [🛠️ Development Setup](#-development-setup)
- [✅ Code Standards](#-code-standards)
- [🧪 Testing Expectations](#-testing-expectations)
- [🔀 Pull Requests](#-pull-requests)
- [🐛 Reporting Issues](#-reporting-issues)

---

## 🧭 Ground Rules

- 🎯 **One logical change, one PR.** Keep a PR to a single feature, fix, or
  cleanup. Don't bundle unrelated changes.
- 🔒 **Locked decisions are locked.** No agent frameworks, no Redis, Postgres-outbox
  for all agent comms, Slack-only notifications, Vault-only secrets. Change them
  via a **new ADR**, not a silent deviation.
- 📝 **No dump commits.** History should read as a narrative: small, scoped,
  imperative commits (`feat(scope): …`, `test(scope): …`, `docs: …`), not an
  "add everything" commit.
- ⚙️ **Config, not code, for anything tunable.** Correlation rules and plan
  templates are YAML mounted as ConfigMaps. Don't hardcode what the plan says is config.

---

## 🛠️ Development Setup

Full spec: [docs/implementation_plan.md](docs/implementation_plan.md). The short
version:

```bash
scripts/bootstrap.sh     # generates .env with per-machine credentials, installs uv
make setup               # prepare the uv workspace
make dev-infra-up        # bring up the local infra stack
```

> 🔑 `bootstrap.sh` fills the generated secrets; you still set the ones it can't
> invent (e.g. `OPENAI_API_KEY`) in `.env` yourself.

🚀 New to the project? The [15-minute quickstart](docs/quickstart.md) is the
fastest way to a running system.

---

## ✅ Code Standards

| Rule | Detail |
|---|---|
| 🐍 **Language** | Python 3.14 target (3.12+ minimum), `uv` for dependencies |
| 🎨 **Lint + types** | `ruff check .` and `mypy .` must pass (`make lint`) |
| 🧪 **Tests** | `pytest` must pass (`make test`) |
| ❤️ **Every service** | exposes `/healthz`, `/readyz`, `/metrics`; JSON logs via `structlog` with a `correlation_id` on every line; one OTel span per request |
| ⏳ **Every outbound call** | has a timeout and bounded retries |

---

## 🧪 Testing Expectations

New logic ships with tests **in the same PR.** Agent logic in particular must
cover the three invariants from the plan:

- ⚛️ **Outbox atomicity**: no incident without its outbox event, or vice versa.
- 🔐 **Poller isolation**: two pollers never double-process an event (`SKIP LOCKED`).
- 🔁 **Idempotency**: replaying an already-processed event is a no-op.

> 💡 Where a guarantee is critical, prove it mutation-style: the test must **fail**
> if the guarantee is removed.

---

## 🔀 Pull Requests

Outside contributors work through a fork: fork the repo, create a branch on
your fork, commit your change, then open a PR against the default branch. A
maintainer reviews it and merges when it's ready.

- 🏷️ Describe what the PR changes and why.
- 🟢 CI (lint, typecheck, test, multi-arch build) and the security scan
  (OSV-Scanner) are required checks and must pass before merge.
- 🔑 A first-time contributor's workflow runs wait for maintainer approval to
  start — GitHub's default for fork PRs.
- 📦 Keep the PR scoped to one logical change.

---

## 🐛 Reporting Issues

Open a **GitHub issue** with:

- what you expected vs. what happened,
- the service involved, and
- steps to reproduce (an alert payload, a `make` command, logs).
