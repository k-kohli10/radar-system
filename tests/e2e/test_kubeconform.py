"""kubeconform static validation of all Phase 10 k8s manifests — with teeth.

Runs in the DEFAULT suite: it is `infra` but not `live`, so `addopts = -m 'not
live'` includes it in `make test` and CI, and `make test-quick` drops it (the
step-5 fast-lane pattern). Without a working Docker it FAILS — it does not skip:
a validator's proof that silently skips when its dependency is down is a false
green.

WHY THE RED CASES MATTER. kubeconform EXITS 0 on empty input ("0 resources
found") — point it at a mis-globbed or emptied file list and it passes having
validated nothing. That silent empty pass, with a validator's credibility on top,
is the trap scripts/kubeconform-phase10.sh guards with an exact-count assertion.
So this proves the guard three RED ways — empty, broken, strict-violation — not
only that the real manifests pass green.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.infra

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "kubeconform-phase10.sh"


def _docker_ok() -> bool:
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "info"], capture_output=True).returncode == 0


@pytest.fixture(autouse=True)
def _require_docker() -> None:
    if not _docker_ok():
        pytest.fail(
            "Docker unavailable; the kubeconform proof runs in the default suite "
            "and must fail loud, not skip"
        )


def _run(manifests: str | None) -> subprocess.CompletedProcess[str]:
    """Run the real script; `manifests` overrides the file list for the teeth cases."""
    env = dict(os.environ)
    if manifests is not None:
        env["PHASE10_MANIFESTS"] = manifests
    return subprocess.run(
        ["bash", str(SCRIPT)], capture_output=True, text=True, env=env, cwd=REPO
    )


def _with_temp_manifest(rel: str, content: str) -> subprocess.CompletedProcess[str]:
    """Write a throwaway manifest under the repo (script mounts it), run, clean up."""
    path = REPO / rel
    path.write_text(content)
    try:
        return _run(rel)
    finally:
        path.unlink(missing_ok=True)


def test_all_phase10_manifests_are_valid() -> None:
    result = _run(None)  # the hardcoded, curated list
    assert result.returncode == 0, result.stdout + result.stderr
    assert "16/16 resources valid across 4 manifests" in result.stdout


def test_teeth_empty_run_is_caught() -> None:
    # kubeconform alone exits 0 here; the exact-count guard is the only thing that
    # turns an empty run red.
    result = _run("")
    assert result.returncode == 1, result.stdout + result.stderr
    assert "validated nothing" in result.stdout


def test_teeth_broken_manifest_is_invalid() -> None:
    result = _with_temp_manifest(
        "deploy/otel/_teeth_broken.yaml",
        "apiVersion: apps/v1\nkind: DaemonSet\nmetadata:\n  name: bad\nspec: {}\n",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "INVALID k8s" in result.stdout


def test_teeth_strict_violation_is_invalid() -> None:
    result = _with_temp_manifest(
        "deploy/otel/_teeth_strict.yaml",
        "apiVersion: v1\nkind: ConfigMap\nmetadata:\n  name: x\n"
        "bogus: 1\ndata:\n  a: b\n",
    )
    assert result.returncode == 1, result.stdout + result.stderr
    assert "INVALID k8s" in result.stdout
    # --strict specifically: an unknown field is rejected, not ignored.
    assert "additionalProperties" in result.stdout
