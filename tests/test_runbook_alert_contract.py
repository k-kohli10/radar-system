"""Every runbook's frontmatter must join to something that actually exists.

The runbook corpus in ``docs/runbooks/`` and the alert rules in
``deploy/prometheus/alerting-rules.yml`` are joined only by strings:
``alert_name``, the entries in ``services``, and ``severity``. Nothing at
runtime validates that join. Retrieval is triggered by an incident and matches
on service name and alert name, so a typo on either side does not raise — it
produces a runbook that is simply never retrieved, for any incident, forever.
The corpus looks healthy, the rule looks healthy, and no test fails.

That is the same silent-join failure mode as
``apps/platform-sim/tests/test_rule_metric_contract.py``, which joins rule
expressions to exposed metrics. This module is its counterpart one level up:
rules to runbooks.

Both directions are checked, and together they make the Tier-1 coupling
bijective:

- FORWARD (every runbook resolves to a real rule) catches a runbook describing an
  alert nothing can fire — dead corpus no incident will ever retrieve.
- REVERSE (every alert has a Tier-1 runbook) catches a fireable alert with no
  corpus behind it — the alert fires, retrieval finds nothing, and the reasoner
  quietly falls back to a template RCA. Nothing errors; the incident just gets a
  worse answer.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

_ROOT = Path(__file__).resolve().parent.parent
RUNBOOKS_DIR = _ROOT / "docs" / "runbooks"
RULES_FILE = _ROOT / "deploy" / "prometheus" / "alerting-rules.yml"

#: Frontmatter fields every runbook carries. ``alert_name`` is deliberately absent:
#: it is Tier-1 only (see docs/runbooks/README.md).
REQUIRED_FIELDS = ("runbook_id", "title", "services", "severity", "status")

#: The section structure from the README. Order matters and is asserted: an H2
#: section is the unit of retrieval, so a renamed or dropped section silently
#: changes what gets chunked and embedded.
REQUIRED_SECTIONS = (
    "Summary",
    "Symptoms",
    "Impact",
    "Likely Causes",
    "Investigation",
    "Resolution",
    "Escalation",
    "Related",
)

VALID_SEVERITIES = {"critical", "high", "medium", "low"}
VALID_STATUSES = {"fixture", "reviewed"}

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)


def _runbook_paths() -> list[Path]:
    """Every runbook file. README.md documents the corpus, it is not part of it."""
    return sorted(p for p in RUNBOOKS_DIR.glob("*.md") if p.name != "README.md")


def _parse(path: Path) -> tuple[dict[str, Any], str]:
    """Split a runbook into (frontmatter, body)."""
    match = _FRONTMATTER.match(path.read_text())
    assert match is not None, (
        f"{path.name} has no YAML frontmatter block. Every runbook must open with "
        f"one: it carries the join keys (see docs/runbooks/README.md)."
    )
    parsed = yaml.safe_load(match.group(1))
    assert isinstance(parsed, dict), f"{path.name} frontmatter is not a YAML mapping"
    return parsed, match.group(2)


def _rules() -> dict[str, dict[str, str]]:
    """``alert name -> labels`` for every declared rule."""
    parsed = yaml.safe_load(RULES_FILE.read_text())
    return {
        rule["alert"]: rule["labels"]
        for group in parsed["groups"]
        for rule in group["rules"]
    }


def _services_in_rules() -> set[str]:
    return {labels["service"] for labels in _rules().values()}


def test_the_corpus_is_not_empty() -> None:
    """Guard the guard.

    Every other test here is parametrized over ``_runbook_paths()``. If that
    glob ever returns nothing — directory renamed, files moved, extension
    changed — pytest generates zero cases and the whole module passes green
    while checking nothing at all. This is the one test that fails in that case.
    """
    assert _runbook_paths(), (
        f"no runbooks found in {RUNBOOKS_DIR}. Either the corpus moved or the "
        f"glob is wrong — and every join test in this module just silently "
        f"stopped checking anything."
    )


@pytest.mark.parametrize("path", _runbook_paths(), ids=lambda p: p.stem)
def test_runbook_frontmatter_has_every_required_field(path: Path) -> None:
    frontmatter, _ = _parse(path)

    for field in REQUIRED_FIELDS:
        assert field in frontmatter, f"{path.name} frontmatter is missing {field!r}"


@pytest.mark.parametrize("path", _runbook_paths(), ids=lambda p: p.stem)
def test_runbook_id_matches_the_filename(path: Path) -> None:
    """``runbook_id`` is the identity used in Postgres and in the ES chunk ids.

    Letting it drift from the filename means the manifest and the file on disk
    disagree about which runbook this is.
    """
    frontmatter, _ = _parse(path)

    assert frontmatter["runbook_id"] == path.stem, (
        f"{path.name} declares runbook_id={frontmatter['runbook_id']!r} but the "
        f"filename stem is {path.stem!r}."
    )


@pytest.mark.parametrize("path", _runbook_paths(), ids=lambda p: p.stem)
def test_runbook_severity_and_status_use_the_declared_vocabulary(path: Path) -> None:
    frontmatter, _ = _parse(path)

    assert frontmatter["severity"] in VALID_SEVERITIES, (
        f"{path.name} has severity={frontmatter['severity']!r}, "
        f"not one of {sorted(VALID_SEVERITIES)}"
    )
    assert frontmatter["status"] in VALID_STATUSES, (
        f"{path.name} has status={frontmatter['status']!r}, "
        f"not one of {sorted(VALID_STATUSES)}"
    )


@pytest.mark.parametrize("path", _runbook_paths(), ids=lambda p: p.stem)
def test_runbook_services_appear_as_a_service_label_in_the_rules(path: Path) -> None:
    """``services`` pre-filters retrieval, so a bad entry silently hides a runbook.

    Applies to depth runbooks too: they carry no ``alert_name``, but their
    service names are still the pre-filter key, and a typo there is exactly as
    invisible.
    """
    frontmatter, _ = _parse(path)
    known = _services_in_rules()

    declared = frontmatter["services"]
    assert isinstance(declared, list) and declared, (
        f"{path.name} must declare `services` as a non-empty list"
    )
    for service in declared:
        assert service in known, (
            f"{path.name} declares service {service!r}, which appears on no alert "
            f"rule. Incidents carry the rule's service label, so nothing will ever "
            f"pre-filter to this runbook. Known services: {sorted(known)}"
        )


def _tier1_paths() -> list[Path]:
    """Runbooks that name an alert. Depth runbooks have no ``alert_name``."""
    return [p for p in _runbook_paths() if "alert_name" in _parse(p)[0]]


@pytest.mark.parametrize("path", _tier1_paths(), ids=lambda p: p.stem)
def test_tier1_runbook_names_an_alert_that_exists(path: Path) -> None:
    frontmatter, _ = _parse(path)
    alert_name = frontmatter["alert_name"]

    assert alert_name in _rules(), (
        f"{path.name} documents alert {alert_name!r}, which no rule declares. "
        f"No incident will ever carry that alert name, so this runbook is dead "
        f"corpus. Known alerts: {sorted(_rules())}"
    )


@pytest.mark.parametrize("path", _tier1_paths(), ids=lambda p: p.stem)
def test_tier1_runbook_agrees_with_its_alert_on_service_and_severity(
    path: Path,
) -> None:
    """The runbook and the rule must describe the same alert, not merely name it.

    A runbook that names a real alert but attributes it to the wrong service is
    worse than one that names nothing: it pre-filters to a service whose
    incidents never carry that alert, so it stays unretrievable while looking
    correctly wired.
    """
    frontmatter, _ = _parse(path)
    labels = _rules()[frontmatter["alert_name"]]

    assert labels["service"] in frontmatter["services"], (
        f"{path.name} documents {frontmatter['alert_name']!r}, which fires with "
        f"service={labels['service']!r}, but the runbook declares services="
        f"{frontmatter['services']!r}."
    )
    assert frontmatter["severity"] == labels["severity"], (
        f"{path.name} declares severity={frontmatter['severity']!r} but "
        f"{frontmatter['alert_name']!r} fires at severity={labels['severity']!r}."
    )


def test_the_alert_rules_are_not_empty() -> None:
    """Guard the reverse direction's collection, for the same reason as above.

    The reverse test is parametrized over the ALERTS, so an alert list that
    parses to nothing — file moved, structure changed, a `groups:` key renamed —
    would generate zero cases and pass green while asserting nothing about
    runbook coverage.
    """
    assert _rules(), (
        f"no alert rules parsed from {RULES_FILE}. The reverse-direction test "
        f"just silently stopped checking that alerts have runbooks."
    )


@pytest.mark.parametrize("alert", sorted(_rules()))
def test_every_alert_has_a_tier1_runbook(alert: str) -> None:
    """The reverse direction: a fireable alert with no runbook is a silent gap.

    ASSUMPTION ENCODED HERE: every alert in the rules file is Tier-1, i.e. every
    alert deserves a runbook. That is true by design today — the six alerts and
    the six Tier-1 runbooks are 1:1 (docs/runbooks/README.md). If an alert is
    ever added that deliberately has no runbook, this test is what will fail, and
    the fix is to make that exemption explicit here rather than to delete the
    check. See the failure message.
    """
    documented = {
        frontmatter["alert_name"]
        for path in _tier1_paths()
        if (frontmatter := _parse(path)[0])
    }

    assert alert in documented, (
        f"alert {alert!r} can fire but no runbook documents it. An incident from "
        f"it will retrieve nothing and the reasoner will fall back to a template "
        f"RCA — no error, just a worse answer.\n"
        f"This test assumes every alert is Tier-1 (today they are, 1:1). If "
        f"{alert!r} is deliberately runbook-less, add an explicit exemption here "
        f"rather than removing the check."
    )


@pytest.mark.parametrize("path", _runbook_paths(), ids=lambda p: p.stem)
def test_runbook_has_the_required_sections_in_order(path: Path) -> None:
    """The H2 sections ARE the chunk boundaries (docs/runbooks/README.md).

    Renaming or dropping one does not fail anything at index time — it just
    changes what gets embedded, quietly. Asserting the structure keeps chunking
    uniform across the corpus so retrieval quality reflects content rather than
    structural accidents.
    """
    _, body = _parse(path)
    sections = tuple(re.findall(r"^## (.+)$", body, re.M))

    assert sections == REQUIRED_SECTIONS, (
        f"{path.name} has sections {sections!r}, expected {REQUIRED_SECTIONS!r}. "
        f"These are the chunk boundaries; changing them changes the index."
    )
