"""Every cross-reference in the operations runbooks must resolve to a real thing.

The runbooks in ``docs/operations/`` are only useful if the alert names, metrics,
make targets and dashboards they tell an on-call engineer to look at actually
exist. Nothing at runtime couples prose to code, so a renamed metric or a dropped
make target would leave a runbook quietly pointing at nothing — the same
silent-join failure ``tests/test_runbook_alert_contract.py`` guards for the
knowledge corpus, one level up: RADAR's own ops docs to RADAR's own artifacts.

Fast and dependency-free (reads files, no Docker), so it runs in the quick loop
and the full suite alike.
"""

from __future__ import annotations

import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_OPS = _ROOT / "docs" / "operations"
_RUNBOOKS = sorted(_OPS.glob("*.md"))
_TEXT = "\n".join(p.read_text() for p in _RUNBOOKS)


def test_there_are_runbooks_to_check() -> None:
    # Non-empty guard: an empty glob would make every assertion below vacuous.
    names = {p.name for p in _RUNBOOKS}
    assert {
        "llm-gateway-failure.md",
        "outbox-backlog.md",
        "vault-secret-rotation.md",
    } <= names, names


def test_cited_alerts_are_defined() -> None:
    rules = (_ROOT / "deploy" / "prometheus" / "radar-service-alerts.yml").read_text()
    for alert in ("LLMTemplateFallbackActive", "OutboxBacklogHigh", "RadarAgentDown"):
        if alert in _TEXT:
            assert f"alert: {alert}" in rules, f"runbook cites undefined alert {alert}"


def test_cited_metrics_are_defined() -> None:
    metrics_src = (
        _ROOT / "packages" / "telemetry" / "src" / "radar_telemetry" / "metrics.py"
    ).read_text()
    cited = sorted(set(re.findall(r"radar_[a-z_]+", _TEXT)))
    assert cited, "expected the runbooks to cite radar_* metrics"
    missing = [m for m in cited if m not in metrics_src]
    assert not missing, f"runbooks cite metrics not defined in telemetry: {missing}"


def test_cited_make_targets_exist() -> None:
    makefile = (_ROOT / "Makefile").read_text()
    targets = {
        line.split(":", 1)[0]
        for line in makefile.splitlines()
        if re.match(r"^[a-zA-Z_-]+:", line)
    }
    cited = sorted(set(re.findall(r"make ([a-z][a-z-]+)", _TEXT)))
    assert cited, "expected the runbooks to cite make targets"
    missing = [t for t in cited if t not in targets]
    assert not missing, f"runbooks cite make targets that do not exist: {missing}"


def test_cited_dashboards_exist() -> None:
    for dash in ("llm-gateway", "incident-pipeline", "outbox-health", "radar-overview"):
        if f"`{dash}`" in _TEXT:
            path = _ROOT / "deploy" / "grafana" / "dashboards" / f"{dash}.json"
            assert path.exists(), f"runbook cites missing dashboard {dash!r}"
