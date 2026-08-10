"""Teeth for scripts/assert-required-tests-ran.py — the CI no-silent-skip guard.

The guard exists so a CI run cannot go green while a load-bearing infra/DB proof
silently skipped (Postgres/Docker/egress/Elasticsearch absent). These tests pin
the three outcomes the guard must distinguish: a required proof that RAN (no
violation), one that SKIPPED (violation), and one ABSENT from the report
(violation). A guard that never reports a violation is not a guard.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "scripts" / "assert-required-tests-ran.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("assert_required_tests_ran", SCRIPT)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


guard = _load()


def test_all_required_ran_no_violations() -> None:
    cases = [
        guard.Case(classname=f"pkg.tests.{module}", name="t", skipped=False)
        for module in guard.REQUIRED_MODULES
    ]
    assert guard.find_violations(cases) == []


def test_skipped_required_is_a_violation() -> None:
    cases = [
        guard.Case(
            classname="packages.database.tests.test_outbox_atomicity",
            name="test_incident_and_outbox_roll_back_together",
            skipped=True,
        )
    ]
    violations = guard.find_violations(cases, ("test_outbox_atomicity",))
    assert len(violations) == 1
    assert violations[0][0] == "test_outbox_atomicity"
    assert "SKIPPED" in violations[0][1]


def test_absent_required_is_a_violation() -> None:
    # An empty report — pytest collected nothing for the required module.
    violations = guard.find_violations([], ("test_kubeconform",))
    assert len(violations) == 1
    assert "NOT RUN" in violations[0][1]


def test_partial_skip_within_a_module_is_a_violation() -> None:
    cases = [
        guard.Case(classname="a.test_idempotency", name="ran", skipped=False),
        guard.Case(classname="a.test_idempotency", name="skipped", skipped=True),
    ]
    violations = guard.find_violations(cases, ("test_idempotency",))
    assert len(violations) == 1
    assert "1/2" in violations[0][1]


def test_parse_cases_reads_skipped_flag_from_xml(tmp_path: Path) -> None:
    xml = (
        '<testsuite name="pytest" tests="2">'
        '<testcase classname="a.test_ran" name="t1" time="0.1"/>'
        '<testcase classname="a.test_skipped" name="t2" time="0.0">'
        '<skipped type="pytest.skip" message="dep down"/>'
        "</testcase>"
        "</testsuite>"
    )
    report = tmp_path / "junit.xml"
    report.write_text(xml)
    cases = guard.parse_cases([report])
    by_class = {c.classname: c.skipped for c in cases}
    assert by_class == {"a.test_ran": False, "a.test_skipped": True}
