# Phase gates

RADAR is built in a fixed sequence of phases (Phase 0 through Phase 14) defined
in [docs/implementation_plan.md](../../docs/implementation_plan.md). Each phase
has an explicit list of deliverables and done-conditions. The plan is the source
of truth; this file only sets the discipline for staying inside it.

## Rules

- **One phase at a time.** Implement only the current phase's listed
  deliverables. Do not pull anything forward from a future phase unless I
  explicitly ask for it.
- **If the active phase/task is unknown, ask.** Do not guess which phase we are
  in.
- **Flag deferred/out-of-scope config, never silently implement it.** If a
  config option, flag, or feature belongs to a later phase or is explicitly
  deferred, surface it and leave it out — do not quietly wire it in.

## What "staying in scope" means

> Staying in scope means: do not add new features, config options, or
> architecture outside the current phase's deliverables. It does NOT mean
> ignoring bugs, contradictions, or gaps discovered while implementing the
> current phase. If you find one: stop, flag it explicitly, and if approved,
> fix it in a separate clearly-labeled commit (e.g. `fix: ...`) before
> continuing feature work — same pattern as commit a288a4e in this repo's
> history, where a prior-phase ingestion gap was fixed in a standalone surgical
> commit during Phase 7.
