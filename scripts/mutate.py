#!/usr/bin/env python
"""Mutation harness: break one guarantee, prove the load-bearing test goes red, restore.

A test that passes proves nothing on its own. It has to FAIL when the thing it guards is
broken — otherwise it is a test-shaped comment, and the suite is certifying code nobody
has actually checked. So every guard in this repo gets mutated: change the behaviour it
protects, and watch its test go red.

WHY THIS IS A TOOL AND NOT A HABIT
----------------------------------
Three times now, a hand-rolled mutation has quietly certified nothing:

1. **Dead code the suite could not see.** The planner's duplicate pre-check was deleted
   and NO test changed — the unique index absorbed the sequential duplicate too, so the
   outcome was identical and the pre-check was, as far as the tests knew, dead.
2. **A guard tested only through the layer below it.** The reasoner's race path was
   exercised via ``store_recommendation`` directly, so deleting the HANDLER's entire
   ``except IntegrityError`` block changed nothing. 184 tests passed over dead code.
3. **A mutation applied to the WRONG SITE.** ``await mark_processed(...)`` followed by
   ``await session.commit()`` appears twice in the reasoner's handler at identical
   indentation. A ``str.replace(old, new, 1)`` hit the first one — the unhandled-event
   branch — not the write path. The run came back green and would have "proved" that
   post-commit metric placement does not matter. It is the opposite of the truth.

Case 3 is the dangerous one, and it is the reason for :func:`_locate`. A green run that
certifies NOTHING is indistinguishable from a passing test. Checking that the anchor is
PRESENT does not catch it — the anchor was present, twice. Only checking that it is
**UNIQUE** catches it.

So: an ambiguous anchor is a HARNESS FAILURE, not a mutation that quietly lands
somewhere else. The wrong thing is made unrepresentable, which is the same rule this
repo applies to its own code — now applied to the tooling that checks it.

    A mutation that changes no assertion means the tests are blind or the code is dead.
    Find out which. Never assume the third possibility — that the mutation did not
    actually apply — is impossible: twice now it was the answer.

RUN IT ON THE SIMPLE ONES TOO. The simplicity of the CHANGE says nothing about the
uniqueness of the TARGET: a one-line increment is trivial, but if that line appears
twice, the anchor is ambiguous and the harness is the only thing that will tell you.

USAGE
-----
    from mutate import Mutation, run_mutations

    run_mutations(
        tests="apps/reasoner-agent/tests/test_reasoner_metrics.py",
        mutations=[
            Mutation(
                name="increment BEFORE the commit",
                expect="the rollback test goes red",
                file="apps/reasoner-agent/src/radar_reasoner_agent/routes.py",
                # Anchors must be UNIQUE in the file. Include enough surrounding lines.
                anchor='    outcome=outcome,\n)\nawait mark_processed(',
                replacement="...",
            ),
        ],
    )

Every file is restored byte-for-byte afterwards, including when a mutation raises.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class AmbiguousAnchorError(AssertionError):
    """The anchor matches more than once — the harness refuses to guess.

    THE bug this tool exists to prevent. ``str.replace(old, new, 1)`` would silently
    mutate the first match, which may not be the site under test; the suite then passes,
    and the passing run is read as proof that the mutated guard does not matter.

    Fix the ANCHOR (widen it until it is unique), never the assertion.
    """


class MissingAnchorError(AssertionError):
    """The anchor matches nothing — the mutation would be a no-op."""


@dataclass(frozen=True)
class Mutation:
    """One deliberate break, and the test that must notice it."""

    #: What is being broken, in a reviewer's words: "increment before the commit".
    name: str
    #: What must happen. "the rollback test goes red". Printed next to the result so a
    #: run that does not match the expectation is obvious at a glance.
    expect: str
    file: str
    #: Text to replace. MUST occur exactly once in the file — see the module docstring.
    anchor: str
    #: What to replace it with. Empty string deletes the anchor (removing a guard).
    replacement: str = ""


@dataclass
class _Result:
    mutation: Mutation
    line: str
    killed: list[str] = field(default_factory=list)

    @property
    def survived(self) -> bool:
        """No test died. Either the tests are blind, or the code is dead."""
        return not self.killed


def _locate(path: Path, anchor: str) -> None:
    """Refuse anything but exactly one match. The whole point of the harness.

    PRESENT is not enough. The anchor that broke us was present — twice.
    """
    text = path.read_text()
    hits = text.count(anchor)
    if hits == 0:
        raise MissingAnchorError(
            f"anchor not found in {path}; the mutation would change nothing and the "
            f"green run would prove nothing:\n  {anchor!r}"
        )
    if hits > 1:
        raise AmbiguousAnchorError(
            f"anchor matches {hits} sites in {path} — the harness will NOT guess which "
            f"one you meant. A replace(..., 1) would silently mutate the first, and a "
            f"green run would 'prove' the guard does not matter.\n"
            f"Widen the anchor until it is unique:\n  {anchor!r}"
        )


def _pytest(tests: str) -> tuple[str, list[str]]:
    proc = subprocess.run(
        ["uv", "run", "pytest", tests, "-q", "--no-header", "-p", "no:warnings"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    out = proc.stdout + proc.stderr
    summary = next(
        (
            line.strip()
            for line in reversed(out.splitlines())
            if "passed" in line or "failed" in line or "error" in line
        ),
        "(no summary)",
    )
    killed = sorted(
        {
            line.split(" ")[1].split("::")[-1].split("[")[0]
            for line in out.splitlines()
            if line.startswith("FAILED ")
        }
    )
    return summary, killed


def run_mutations(*, tests: str, mutations: list[Mutation]) -> int:
    """Apply each mutation in turn, run ``tests``, restore. Returns an exit code.

    Non-zero if ANY mutation survived — a surviving mutation is a hole, and the harness
    is not a report you read at your leisure, it is a gate.
    """
    originals = {m.file: (ROOT / m.file).read_text() for m in mutations}

    # Every anchor is validated BEFORE anything is touched, so an ambiguous anchor
    # cannot leave a half-mutated tree behind.
    for mutation in mutations:
        _locate(ROOT / mutation.file, mutation.anchor)

    def restore() -> None:
        for name, text in originals.items():
            (ROOT / name).write_text(text)

    results: list[_Result] = []
    try:
        baseline, _ = _pytest(tests)
        print(f"BASELINE  {baseline}\n")
        if "failed" in baseline or "error" in baseline:
            print("!! the suite is not green before mutating. Fix that first.")
            return 1

        for mutation in mutations:
            path = ROOT / mutation.file
            path.write_text(
                originals[mutation.file].replace(
                    mutation.anchor, mutation.replacement, 1
                )
            )
            summary, killed = _pytest(tests)
            restore()

            result = _Result(mutation, summary, killed)
            results.append(result)

            status = "SURVIVED" if result.survived else "killed"
            print(f"[{status}]  {mutation.name}")
            print(f"          expect: {mutation.expect}")
            print(f"          {summary}")
            if killed:
                print(f"          red: {', '.join(killed)}")
            print()
    finally:
        restore()

    final, _ = _pytest(tests)
    for name, text in originals.items():
        assert (ROOT / name).read_text() == text, f"{name} was NOT restored"
    print(f"RESTORED  {final}  (all files byte-identical)\n")

    survivors = [r for r in results if r.survived]
    if survivors:
        print("SURVIVING MUTATIONS — the tests are blind, or the code is dead:")
        for r in survivors:
            print(f"  - {r.mutation.name}")
        print("\nFind out WHICH. Do not assume the mutation simply 'did not matter'.")
        return 1

    print(f"All {len(results)} mutations killed.")
    return 0


if __name__ == "__main__":
    print(__doc__)
    sys.exit(0)
