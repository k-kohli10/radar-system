#!/usr/bin/env python3
"""Path-based, dependency-aware changed-service detection for RADAR CI.

Emits the set of application services whose Docker image must be rebuilt for a
given set of changed files. This is the mechanism ADR 0018 leans on: it retired
the separate radar-infra repository on the argument that path-based CI delivers
the same release-cadence isolation the split existed for. The load-bearing
property is that a change under ``deploy/`` (or ``docs/``) triggers ZERO
application builds — that is what makes single-repo cadence isolation real rather
than asserted. ``tests/ci/test_detect_changed_services.py`` pins it with teeth.

Detection is DEPENDENCY-AWARE, not a naive path-prefix match. A change under a
shared workspace member (``packages/contracts``, a plugin) fans out to every
service whose runtime dependency closure includes it — because those services
bundle that code into their image. A blunt prefix matcher would wrongly rebuild
one service, or none, for a shared-library change.

Rules (default is NO build; only a recognized trigger adds a service):
  - ``apps/<svc>/**``            -> build <svc>            (if <svc> is buildable)
  - ``packages/<pkg>/**``        -> build every buildable service that
    ``plugins/<group>/<impl>/**``   transitively depends on that member (fan-out)
  - a file in GLOBAL_ROOT_FILES  -> build ALL buildable services (shared
                                    dependency/context: every image changes)
  - ``deploy/**``, ``docs/**``,  -> build NOTHING
    anything else

Only ``[project].dependencies`` count for the closure (runtime deps that land in
the image). Dev-only members like ``radar-testing`` live in
``[dependency-groups].dev`` and therefore never trigger an image rebuild — the
test suite runs unconditionally in CI, so test-fixture changes need no image.

Usage:
    git diff --name-only <base> <head> | scripts/detect-changed-services.py
    scripts/detect-changed-services.py apps/ingestion/foo.py deploy/x.yaml
    scripts/detect-changed-services.py --format lines < changed.txt

Default output is a JSON array of service names (suitable for a GitHub Actions
matrix via ``fromJSON``). ``--format lines`` prints one per line for humans.
"""

from __future__ import annotations

import json
import re
import sys
import tomllib
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent

# apps/ directories that are NOT deployable images and so never enter the build
# matrix. platform-sim is a local-only e2e simulator (it fires alerts at
# ingestion for the scrape->fire->webhook proof); it is not in the helm chart or
# the Makefile APPS list and is never deployed. It DOES have a Dockerfile, so it
# must be excluded by name rather than by "has a Dockerfile".
BUILD_EXCLUDE = frozenset({"platform-sim"})

# The EXHAUSTIVE set of repo-root files whose change invalidates every image:
# the shared lock, the workspace root manifest, and the build-context filter.
# This is a deliberate touch-point (same discipline as the kubeconform script's
# hardcoded counts): if a new root file starts affecting image contents, add it
# here ON PURPOSE. Anything not listed here and not under a recognized directory
# triggers no build.
GLOBAL_ROOT_FILES = frozenset({"pyproject.toml", "uv.lock", ".dockerignore"})


@dataclass(frozen=True)
class Workspace:
    """The uv workspace dependency graph, loaded from pyproject files."""

    dir_to_name: dict[str, str]  # "packages/contracts" -> "radar-contracts"
    deps: dict[str, frozenset[str]]  # package name -> its workspace dep names
    buildable: frozenset[str]  # app basenames that produce a deployable image

    def transitive_deps(self, name: str) -> frozenset[str]:
        """Every workspace member ``name`` depends on, transitively."""
        seen: set[str] = set()
        stack = list(self.deps.get(name, ()))
        while stack:
            dep = stack.pop()
            if dep in seen:
                continue
            seen.add(dep)
            stack.extend(self.deps.get(dep, ()))
        return frozenset(seen)

    def dependents(self, member_name: str) -> set[str]:
        """Buildable app basenames whose closure includes ``member_name``."""
        result: set[str] = set()
        for app in self.buildable:
            app_pkg = self.dir_to_name[f"apps/{app}"]
            if member_name in self.transitive_deps(app_pkg):
                result.add(app)
        return result


def _bare_name(dep: str) -> str:
    """ "sqlalchemy[asyncio]==2.0.51" -> "sqlalchemy"; "radar-contracts" -> same."""
    return re.split(r"[\[\]<>=!~; ]", dep.strip(), maxsplit=1)[0]


def _member_dirs(repo_root: Path) -> list[str]:
    """Workspace member directories, per the root pyproject members globs."""
    dirs: list[str] = []
    for pattern in ("apps/*", "packages/*"):
        dirs += [str(p.relative_to(repo_root)) for p in repo_root.glob(pattern)]
    dirs += [str(p.relative_to(repo_root)) for p in repo_root.glob("plugins/*/*")]
    return [d for d in dirs if (repo_root / d / "pyproject.toml").is_file()]


def load_workspace(repo_root: Path = REPO_ROOT) -> Workspace:
    """Parse every workspace member's pyproject into a dependency graph."""
    dir_to_name: dict[str, str] = {}
    raw_deps: dict[str, list[str]] = {}
    for member in _member_dirs(repo_root):
        data = tomllib.loads((repo_root / member / "pyproject.toml").read_text())
        name = data["project"]["name"]
        dir_to_name[member] = name
        raw_deps[name] = data.get("project", {}).get("dependencies", [])

    member_names = set(dir_to_name.values())
    # Keep only deps that are themselves workspace members (the radar-* graph);
    # third-party pins (fastapi, sqlalchemy, ...) are irrelevant to fan-out.
    deps = {
        name: frozenset(
            _bare_name(d) for d in dep_list if _bare_name(d) in member_names
        )
        for name, dep_list in raw_deps.items()
    }

    buildable = frozenset(
        member.split("/", 1)[1]
        for member in dir_to_name
        if member.startswith("apps/")
        and (repo_root / member / "Dockerfile").is_file()
        and member.split("/", 1)[1] not in BUILD_EXCLUDE
    )
    return Workspace(dir_to_name=dir_to_name, deps=deps, buildable=buildable)


def services_for_changes(
    changed: Iterable[str], workspace: Workspace | None = None
) -> set[str]:
    """The buildable services to rebuild for ``changed`` (a list of repo paths)."""
    ws = workspace if workspace is not None else load_workspace()
    build: set[str] = set()
    for raw in changed:
        path = raw.strip()
        if not path:
            continue
        if path in GLOBAL_ROOT_FILES:
            build |= set(ws.buildable)
            continue
        parts = path.split("/")
        top = parts[0]
        if top == "apps" and len(parts) >= 2:
            if parts[1] in ws.buildable:
                build.add(parts[1])
        elif top == "packages" and len(parts) >= 2:
            name = ws.dir_to_name.get(f"packages/{parts[1]}")
            if name is not None:
                build |= ws.dependents(name)
        elif top == "plugins" and len(parts) >= 3:
            name = ws.dir_to_name.get(f"plugins/{parts[1]}/{parts[2]}")
            if name is not None:
                build |= ws.dependents(name)
        # deploy/**, docs/**, and everything else: NO application build. This is
        # the ADR 0018 load-bearing clause — do not add a fallthrough that builds.
    return build


def _read_changed(argv: list[str]) -> list[str]:
    paths = [a for a in argv if not a.startswith("-")]
    if paths:
        return paths
    return sys.stdin.read().splitlines()


def main(argv: list[str]) -> int:
    fmt = "lines" if "--format" in argv and "lines" in argv else "json"
    services = sorted(services_for_changes(_read_changed(argv)))
    if fmt == "lines":
        print("\n".join(services))
    else:
        print(json.dumps(services))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
