# 🛰️ RADAR

**Real-time Anomaly Detection and Automated Response**

RADAR is an AI-powered incident intelligence platform for SRE workflows. It ingests
pre-fired alerts from Prometheus and Kibana, correlates them into incidents using
configurable rules, retrieves relevant runbooks, reasons over root causes with an LLM,
delivers a structured root cause analysis (RCA) to the on-call engineer in Slack,
collects feedback on it, and answers status queries through a Slack bot.

## Contents

- [The Problem](#-the-problem)
- [Scope](#-scope)
- [How It Works](#-how-it-works)
- [Run It](#-run-it)
- [Domain](#-domain)
- [Status](#-status)
- [Documentation](#-documentation)
- [License](#-license)

---

## 🧩 The Problem

When an alert fires at 3am, the on-call engineer spends the first ten minutes the same
way every time: find the right runbook, correlate it with whatever else fired, work out
what changed recently, and decide where to look first. That triage is repetitive, time
pressured, and rarely captured once the incident closes.

RADAR automates that first ten minutes and keeps the engineer in control. It reaches an
informed starting point faster, with a documented trail of what was correlated, what was
retrieved, and what was recommended.

RCAs stay grounded in your own runbooks. Retrieved excerpts are graded for whether they
actually address the incident, so a close-but-wrong runbook stays out. When the corpus
has nothing that fits, the RCA reasons from the incident and the investigation plan and
says plainly that no runbook covers it. An invented runbook reads exactly like a real one
to an engineer following it at 3am, so RADAR states its grounding every time.

---

## 🎯 Scope

RADAR focuses on incident triage and keeps clear boundaries:

| Boundary | Detail |
|---|---|
| **Detection stays upstream** | Prometheus alerting rules and Kibana Watcher decide when something is wrong. RADAR acts on the alerts they fire. |
| **Recommends, humans execute** | RADAR produces an RCA and recommended actions. A human runs any change against production. |
| **A fixed triage pipeline** | Watcher → Planner → Reasoner is a purpose-built sequence for incident triage. The Reasoner consults the knowledge service for runbook context. |
| **Postgres is the system of record** | Incident state lives in Postgres, with a transactional outbox as the agent message bus. |

---

## 🔧 How It Works

```mermaid
flowchart TD
    P["Prometheus / Kibana Watcher"] -->|pre-fired alert| I[ingestion]
    I -->|normalize, dedupe, outbox| W[watcher-agent]
    W -->|correlate alerts into an incident| PL[planner-agent]
    PL -->|build an investigation plan| R[reasoner-agent]
    R -->|retrieve + grade runbook context| K[knowledge-service]
    K -->|graded excerpts, or nothing relevant| R
    R -->|call LLM, produce RCA| F[feedback-service]
    F -->|deliver Slack card, run Slack bot| S[("Slack")]

    DB[("Postgres<br/>transactional outbox<br/>(all agent comms)")]
    I -.-> DB
    W -.-> DB
    PL -.-> DB
    R -.-> DB
```

Agents coordinate through a Postgres outbox. Every handoff is a row a dedicated outbox
worker picks up and dispatches with retries and idempotency guarantees. See
[docs/architecture/agent-pipeline.md](docs/architecture/agent-pipeline.md).

---

## 🚀 Run It

Two ways to run the full stack locally, both from one bootstrap:

```bash
scripts/bootstrap.sh                # checks tools, installs uv, generates .env
# set OPENAI_API_KEY in .env (and SLACK_* for the Slack bot)
make docker-up                      # bring up the whole stack in Docker
make agent-secrets && make index    # index runbooks: knowledge-service ready, grounded RCAs
```

Then fire an alert and watch the RCA land in Slack and Postgres. Full walkthrough:

- **Docker (two-stack):** [docs/operations/docker.md](docs/operations/docker.md). One command up, plus the end-to-end test.
- **Native dev:** [docs/local-development.md](docs/local-development.md). Services on the host for a fast edit loop.

---

## 🛒 Domain

The target system is a stubbed e-commerce `order-service`. Its realistic failure modes
(order-processing failures, checkout timeouts, inventory latency, payment-gateway errors,
memory pressure) drive the alert scenarios, the runbooks, and the demo narrative. See
[docs/architecture/system-overview.md](docs/architecture/system-overview.md).

---

## 📍 Status

RADAR is built incrementally, one phase at a time, with each phase landing as its own
PR. See [docs/roadmap.md](docs/roadmap.md) for the phase breakdown and
[docs/implementation_plan.md](docs/implementation_plan.md) for the full technical
specification.

---

## 📚 Documentation

| Path | Purpose |
|---|---|
| [`docs/local-development.md`](docs/local-development.md) | Run the stack locally, native or Docker |
| [`docs/operations/docker.md`](docs/operations/docker.md) | The two-stack Docker workflow and end-to-end test |
| [`docs/adr/`](docs/adr/) | Architectural decision records: the why behind each choice |
| [`docs/architecture/`](docs/architecture/) | System overview, agent pipeline, data model, sequence flows, observability |
| [`docs/operations/`](docs/operations/) | Runbooks for operating RADAR itself |
| [`docs/runbooks/`](docs/runbooks/) | Runbooks about the target services RADAR reasons over |
| [`docs/glossary.md`](docs/glossary.md) | Terminology used across the codebase and docs |
| [`docs/roadmap.md`](docs/roadmap.md) | Phase by phase build plan |

---

## 📄 License

MIT. See [LICENSE](LICENSE).
