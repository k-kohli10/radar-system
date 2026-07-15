# RADAR — Claude Code Instructions

RADAR is a multi-agent incident-response system: independent Python/FastAPI
services (ingestion, watcher, planning, notification, and supporting agents)
that detect, enrich, and act on operational incidents. Services never call each
other directly — they coordinate through a Postgres transactional outbox. The
full source of truth for scope, phases, and locked decisions is
[docs/implementation_plan.md](../docs/implementation_plan.md); when in doubt,
that document wins over anything summarized here.

## Hard rules — never to be violated

- **Stack:** Python 3.14, uv workspaces, FastAPI, Pydantic v2, SQLAlchemy async,
  structlog, Postgres. Nothing else fills these roles.
- **No orchestration frameworks:** no LangChain, LangGraph, LiteLLM, or any
  agent/LLM orchestration framework.
- **No extra infrastructure:** no Redis, no Jaeger, no external message broker.
- **Inter-agent communication is Postgres-only:** agents communicate solely via
  the `outbox_events` table. No direct HTTP between agents.
- **Secrets from Vault only:** secrets come exclusively from HashiCorp Vault
  secret files, never from environment variables.
- **Quality gate:** mypy strict and ruff must pass before any commit is proposed.
- **One logical commit at a time.** Stop and wait for my explicit approval
  before starting the next task or task within a phase.
- **Git staging:** after I approve a unit of work, you may stage the relevant
  files with explicit paths (`git add <file1> <file2> ...`). Never use
  `git add -A`, `git add .`, or wildcard staging. You never run `git commit`,
  `git push`, `git branch`, `git merge`, `git rebase`, or any other git command
  — those are mine.
- **Commit format:** Conventional Commits, one logical unit per commit, not per
  file.

## Phase tracking

Work proceeds phase by phase per
[docs/implementation_plan.md](../docs/implementation_plan.md). Implement only the
current phase's listed deliverables. **If I have not told you which phase/task is
active, ask before doing any work.** See
[rules/phase-gates.md](rules/phase-gates.md).

## Rules and agents

- [rules/phase-gates.md](rules/phase-gates.md) — scope discipline, one phase at a time
- [rules/architecture-constraints.md](rules/architecture-constraints.md) — locked architecture
- [rules/testing.md](rules/testing.md) — how tests must prove behavior
- [rules/git-workflow.md](rules/git-workflow.md) — staging and commit workflow
- Agents: [planner](agents/planner.md), [implementer](agents/implementer.md),
  [docs-writer](agents/docs-writer.md)

## Key make commands

- `make setup` — install/prepare the workspace
- `make dev` — run the local dev environment
- `make lint` — ruff + mypy strict
- `make test` — run the test suite
