"""Teeth for scripts/detect-changed-services.py, run against REAL repo paths.

These are not shape checks. They pin the ADR 0018 property that path-based CI
delivers single-repo cadence isolation, and they run the actual detection over
the actual workspace graph — the same code CI runs. The load-bearing case is
``test_deploy_change_builds_nothing``: until it holds, "deploy/-changes-nothing"
is asserted and not proven, and the single-repo decision ships unverified.

To confirm these have teeth (do not just pass on any implementation):
  - break the deploy exclusion — add an ``else: build |= set(ws.buildable)``
    fallthrough in ``services_for_changes`` — and this module goes RED at
    ``test_deploy_change_builds_nothing`` and ``test_mixed_change_ignores_deploy``.
  - break dependency-awareness — make a package change return every service —
    and ``test_shared_lib_fans_out_to_a_real_subset`` goes RED (llm-gateway,
    which has no radar-database dependency, would be wrongly included).
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "detect-changed-services.py"

# The eight deployable services. platform-sim is deliberately excluded (local-only
# e2e simulator, never deployed). Hardcoded on purpose so the fan-out assertions
# below cannot pass vacuously; test_buildable_set_matches_reality pins it to the
# graph the script actually discovers.
ALL_DEPLOYABLE = {
    "feedback-service",
    "ingestion",
    "knowledge-service",
    "llm-gateway",
    "outbox-worker",
    "planner-agent",
    "reasoner-agent",
    "watcher-agent",
}


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("detect_changed_services", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Register before exec: the frozen-annotations dataclass resolves its field
    # types via sys.modules[module_name], which is absent for a file-path import.
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


detect = _load()


def test_buildable_set_matches_reality() -> None:
    """Guard against a vacuous suite: the hardcoded set IS what the script finds."""
    assert detect.load_workspace().buildable == ALL_DEPLOYABLE


def test_app_change_builds_only_that_service() -> None:
    # Case 1: a change under one app builds that app and nothing else.
    changed = ["apps/feedback-service/src/radar_feedback_service/bot.py"]
    assert detect.services_for_changes(changed) == {"feedback-service"}


def test_deploy_change_builds_nothing() -> None:
    # Case 2 (LOAD-BEARING, ADR 0018): a deploy/-only change queues zero builds.
    changed = ["deploy/grafana/dashboards/radar-overview.json"]
    assert detect.services_for_changes(changed) == set()


def test_docs_change_builds_nothing() -> None:
    assert detect.services_for_changes(["docs/roadmap.md"]) == set()


def test_shared_contract_fans_out_to_all_dependents() -> None:
    # Case 3: a shared library every service depends on fans out to ALL of them —
    # not one (naive prefix match), not zero.
    changed = ["packages/contracts/src/radar_contracts/alerts.py"]
    result = detect.services_for_changes(changed)
    assert result == ALL_DEPLOYABLE
    assert len(result) > 1


def test_shared_lib_fans_out_to_a_real_subset() -> None:
    # Proves the fan-out is a genuine dependency graph, not "any package -> all":
    # radar-database is a runtime dep of every deployable service EXCEPT the
    # llm-gateway, so a database change must rebuild seven, never the gateway.
    changed = ["packages/database/src/radar_database/session.py"]
    result = detect.services_for_changes(changed)
    assert "llm-gateway" not in result
    assert result == ALL_DEPLOYABLE - {"llm-gateway"}


def test_dev_only_member_change_builds_nothing() -> None:
    # radar-testing is a dev-group dependency only; it never lands in an image, so
    # a change to it triggers no image build (the test suite runs regardless).
    changed = ["packages/testing/src/radar_testing/postgres.py"]
    assert detect.services_for_changes(changed) == set()


def test_excluded_app_is_never_built() -> None:
    # platform-sim has a Dockerfile but is not a deployed service.
    assert detect.services_for_changes(["apps/platform-sim/src/x.py"]) == set()


def test_global_root_file_rebuilds_everything() -> None:
    # A shared lock change invalidates every image.
    assert detect.services_for_changes(["uv.lock"]) == ALL_DEPLOYABLE


def test_mixed_change_ignores_deploy() -> None:
    # A realistic PR touching both deploy/ and one app builds only the app: the
    # deploy/ half still contributes nothing.
    changed = [
        "deploy/prometheus/prometheus.yml",
        "apps/ingestion/src/radar_ingestion/main.py",
    ]
    assert detect.services_for_changes(changed) == {"ingestion"}
