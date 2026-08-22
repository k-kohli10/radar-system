#!/usr/bin/env python3
"""Fail CI if a load-bearing infra/DB proof was SKIPPED or never ran.

pytest exits 0 when tests SKIP. So a CI run where Postgres never came up, or
Docker/egress/Elasticsearch was unavailable, goes green-with-skips — the exact
false green this whole phase guards against (the Phase 9 "293 skipped hid the
proof" lesson). The fail-loud infra tests already turn a missing Docker into a
RED failure, but the real-Postgres guarantees SKIP CLEANLY when the database is
unreachable (radar_testing.postgres calls pytest.skip), and a skipped
done-condition proof proves nothing while looking green.

This guard closes that gap at the CI layer: given the JUnit XML from the suite,
it fails the run if any REQUIRED proof is skipped or absent from the report.

    pytest -m 'not live' --junitxml=junit.xml
    scripts/assert-required-tests-ran.py junit.xml

Matching is by test-module name against each ``<testcase classname="...">`` (the
dotted module path pytest emits), so it is robust to individual test-function
renames within a required module.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from pathlib import Path

# The proofs that MUST execute for a CI run to be trustworthy. Each is a test
# MODULE name matched as a substring of a JUnit testcase classname. These are the
# tests whose silent skip would be a false green:
#   - real-Postgres guarantees (the Phase 3 bar): skip cleanly without a database.
#   - infra done-condition proofs: need Docker + network egress + Elasticsearch.
# Deliberate touch-point: add a proof here ON PURPOSE when it becomes load-bearing
# (same discipline as the kubeconform script's hardcoded counts).
REQUIRED_MODULES: tuple[str, ...] = (
    "test_outbox_atomicity",
    "test_concurrent_pollers",
    "test_idempotency",
    "test_kubeconform",
    "test_real_prometheus_alert",
    "test_trace_by_correlation_id",
    "test_reasoner_fallback_alert",
)


@dataclass(frozen=True)
class Case:
    classname: str
    name: str
    skipped: bool


def parse_cases(paths: Iterable[Path]) -> list[Case]:
    """Flatten every ``<testcase>`` across the given JUnit XML files."""
    cases: list[Case] = []
    for path in paths:
        root = ET.parse(path).getroot()
        for tc in root.iter("testcase"):
            cases.append(
                Case(
                    classname=tc.get("classname", ""),
                    name=tc.get("name", ""),
                    skipped=tc.find("skipped") is not None,
                )
            )
    return cases


def find_violations(
    cases: Sequence[Case], required: Sequence[str] = REQUIRED_MODULES
) -> list[tuple[str, str]]:
    """Return ``(module, reason)`` for every required proof that did not run.

    A required module is violated if it contributed no testcase to the report
    (not collected — a wrong path, a broken import) or if any of its testcases
    was skipped (its dependency was unavailable). Both are false greens.
    """
    violations: list[tuple[str, str]] = []
    for module in required:
        matches = [c for c in cases if module in c.classname]
        if not matches:
            violations.append(
                (module, "NOT RUN — no testcase in report (not collected)")
            )
            continue
        skipped = [c for c in matches if c.skipped]
        if skipped:
            violations.append(
                (module, f"SKIPPED — {len(skipped)}/{len(matches)} cases skipped")
            )
    return violations


def _parse_required(argv: Sequence[str]) -> tuple[str, ...]:
    for arg in argv:
        if arg.startswith("--require="):
            return tuple(m for m in arg.split("=", 1)[1].split(",") if m)
    return REQUIRED_MODULES


def main(argv: Sequence[str]) -> int:
    paths = [Path(a) for a in argv if not a.startswith("-")]
    if not paths:
        print(
            "usage: assert-required-tests-ran.py <junit.xml> [more.xml ...] "
            "[--require=mod1,mod2]",
            file=sys.stderr,
        )
        return 2
    absent = [str(p) for p in paths if not p.is_file()]
    if absent:
        print(f"ERROR: JUnit report(s) not found: {absent}", file=sys.stderr)
        return 2
    cases = parse_cases(paths)
    if not cases:
        print(
            "ERROR: no <testcase> in report(s) — pytest did not run.", file=sys.stderr
        )
        return 2
    required = _parse_required(argv)
    violations = find_violations(cases, required)
    if violations:
        print("REQUIRED-TEST GUARD: FAIL — a load-bearing proof did not run:")
        for module, reason in violations:
            print(f"  ✗ {module}: {reason}")
        print(
            "\nFALSE GREEN: a required infra/DB proof was skipped or absent, so its "
            "dependency (Postgres, Docker, egress, Elasticsearch) was not available."
        )
        return 1
    print(f"REQUIRED-TEST GUARD: OK — all {len(required)} load-bearing proofs ran.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
