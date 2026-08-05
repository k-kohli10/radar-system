# ADR 0017: Single Repository, Superseding ADR 0001

## Status
Accepted

## Supersedes
ADR 0001 (Two repositories: radar-system + radar-infra)

## Context
ADR 0001 split RADAR into two repositories: radar-system for product code and
radar-infra for platform configuration. The stated benefit was release-cadence
isolation, a platform config change (a Grafana dashboard edit, a Postgres chart
bump) should not trigger an application CI run, and vice versa.

That benefit is real. But it is fully achievable inside a single repository with
path-based CI, which ADR 0001 already commits to building regardless (its own
Consequences section notes the monorepo "requires path-based CI to avoid
rebuilding every service on every commit," and detect-changed-services.py is the
Phase 11 deliverable that provides it). Once path-based CI exists, a change under
deploy/ triggers no application build, delivering the same cadence isolation the
second repo was created for.

Meanwhile the two-repo split carries a cost that has grown load-bearing: RADAR's
end goal is an installable open-source product, and its Phase 14 done-when is a
15-minute quickstart from the README alone. A two-repo layout forces a new user
to clone two repositories and reason about which config lives where, at the exact
moment adoption is won or lost. radar-infra was also never created; the plan has
been referencing a repository that does not exist.

## Decision
RADAR is a single repository: radar-system. The product-versus-platform-config
separation ADR 0001 valued is preserved as top-level directory structure, not as
a repository boundary:

    radar-system/
      apps/          product services
      packages/      shared libraries
      plugins/       swappable backends
      deploy/
        compose/     local dev stack
        helm/        application chart
        prometheus/  alerting rules + scrape config
        grafana/     dashboard ConfigMaps
        otel/        collector config
        fluent-bit/  config
      docs/

radar-infra will not be created. All configuration destined for it lands under
deploy/.

## Consequences
- One clone, one quickstart. Onboarding matches the Phase 14 done-when.
- Cadence isolation is preserved via path-based CI: a change under deploy/
  triggers no application build. The property ADR 0001 wanted, without the
  second repo.
- Removes plan-versus-reality drift; the plan no longer references a
  nonexistent repository.
- If product and platform ever need independent consumption, monorepo to split
  is an afternoon of work (git init, move a directory, update CI paths). No
  current need; deferred until a real user requires it.

## Follow-up
- Flip ADR 0001 Status to "Superseded by ADR 0017."
- Sweep plan references to radar-infra:
  - Locked Decisions "Repos" line in both plan files
  - RADAR_IMPLEMENTATION_PLAN.md:241
  - implementation_plan.md:353
  - Phase 10/11/12 deliverables naming radar-infra
