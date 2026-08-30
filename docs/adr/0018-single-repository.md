# 📦 ADR 0018: Single Repository, Superseding ADR 0001

## Status
Accepted

## Supersedes
ADR 0001 (Two repositories: radar-system + radar-infra)

## Context
ADR 0001 split RADAR into two repositories: radar-system for product code and
radar-infra for platform configuration. The stated benefit was release-cadence
isolation: a platform config change (a Grafana dashboard edit, a Postgres chart
bump) should not trigger an application CI run, and vice versa.

That benefit is real. But it is fully achievable inside a single repository with
path-based CI, which ADR 0001 already commits to building regardless: its own
Consequences section notes the monorepo "requires path-based CI to avoid
rebuilding every service on every commit," and `detect-changed-services.py` is
the Phase 11 deliverable that provides it. Once path-based CI exists, a change
under `deploy/` triggers no application build, delivering the same cadence
isolation the second repo was created for.

Meanwhile the two-repo split carries a cost that has grown load-bearing: RADAR's
end goal is an installable open-source product, and its Phase 14 done-when is a
15-minute quickstart from the README alone. A two-repo layout forces a new user
to clone two repositories and reason about which config lives where, at the exact
moment adoption is won or lost.

radar-infra was also never created. The plan referenced a repository that does
not exist, so retiring it removes plan-versus-reality drift.

## Decision
RADAR is a single repository: radar-system. The product-versus-platform-config
separation ADR 0001 valued is preserved as top-level directory structure, not as
a repository boundary:

```
radar-system/
  apps/                    product services
  packages/                shared libraries
  plugins/                 swappable backends
  deploy/
    compose/               local dev stack
    helm/radar/            application chart
    helm/platform-deps/    postgres, elasticsearch, kibana, prometheus,
                           grafana, vault (values for community charts)
    prometheus/            alerting rules + scrape config
    grafana/               dashboard ConfigMaps
    otel/                  collector config
    fluent-bit/            config
  docs/
```

radar-infra will not be created. All configuration destined for it lands under
`deploy/`.

`deploy/helm/platform-deps/` is called out explicitly because it is the one part
of the radar-infra tree that no existing plan section re-homes. Phase 12's
deliverables name `deploy/helm/radar/` (the application chart) and
`deploy/examples/`, but nothing named a home for the platform dependencies' Helm
values. Without this line those files would have had nowhere to land.

Note that `radar-infra` also names a **Kubernetes namespace**: platform
dependencies, as against the `radar` namespace for app workloads. That namespace
is unaffected by this ADR and keeps its name, including the Vault
init-container's `vault.radar-infra.svc.cluster.local` address from ADR 0007.
Only the repository is retired.

## Consequences
- One clone, one quickstart. Onboarding matches the Phase 14 done-when.
- Cadence isolation is preserved via path-based CI: a change under `deploy/`
  triggers no application build. The property ADR 0001 wanted, without the
  second repo. This makes path-based CI load-bearing for the decision rather
  than merely a build optimization, so Phase 11's done-when asserts it directly
  instead of leaving it implied.
- Removes plan-versus-reality drift; the plan no longer references a
  nonexistent repository.
- If product and platform ever need independent consumption, monorepo to split
  is an afternoon of work (git init, move a directory, update CI paths). No
  current need; deferred until a real user requires it.
