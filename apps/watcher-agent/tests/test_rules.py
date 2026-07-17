"""The correlation rules loader: validation, and what is deliberately inert.

Config-driven behaviour has a signature failure mode: a typo that disables a feature
while looking like it configured one. `suppresion:` with one 's' is not a syntax
error, and a loader that shrugs at unknown keys will happily run with suppression
silently switched off. So the sharpest tests here are the *negative* ones — the
configs that must be REFUSED — and the shipped file is validated as a test in its own
right, because a config that does not load is a pod that does not start.

The deferred rules (window_overrides, service_groups, fingerprint_fields — ADR 0013)
get a test asserting they are parsed and inert. That is not ceremony: an inert field
with nothing pinning it is a trap for whoever next reads the YAML and assumes it
works. This is what makes the deferral a decision rather than an oversight.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from radar_common import ConfigurationError
from radar_contracts import FINGERPRINT_FIELDS, Severity
from radar_watcher_agent.config import WatcherSettings
from radar_watcher_agent.rules import CorrelationRules, load_correlation_rules

SHIPPED_RULES = Path("apps/watcher-agent/config/correlation-rules.yaml")


def _doc(**overrides: Any) -> dict[str, Any]:
    """A minimal valid document, with the block under test overridden."""
    correlation: dict[str, Any] = {
        "default_window_minutes": 5,
        "fingerprint_fields": list(FINGERPRINT_FIELDS),
    }
    correlation.update(overrides)
    return {"correlation": correlation}


def _write(tmp_path: Path, document: Any) -> Path:
    path = tmp_path / "correlation-rules.yaml"
    path.write_text(yaml.safe_dump(document))
    return path


def _load(tmp_path: Path, **overrides: Any) -> CorrelationRules:
    return load_correlation_rules(_write(tmp_path, _doc(**overrides)))


# --- the file that actually ships ---------------------------------------------


def test_the_shipped_rules_file_loads() -> None:
    """The config in the repo must load. A config that doesn't is a pod that won't."""
    rules = load_correlation_rules(SHIPPED_RULES)

    assert rules.default_window_minutes == 5
    assert [r.alert_name for r in rules.suppression] == [
        "OrderServiceHighMemory",
        "InventoryCheckLatency",
    ]
    assert len(rules.escalation) == 1
    assert rules.escalation[0].escalate_to is Severity.CRITICAL


def test_the_settings_default_points_at_the_shipped_file() -> None:
    """The default path must be the file that exists, or startup 503s on a fresh box."""
    assert WatcherSettings().correlation_rules_path == SHIPPED_RULES
    assert SHIPPED_RULES.is_file()


# --- refusing bad config -------------------------------------------------------


def test_an_unknown_key_is_refused(tmp_path: Path) -> None:
    """THE test for config-driven behaviour: a typo must not silently disable a rule.

    `suppresion:` (one 's') parses as valid YAML. Without extra="forbid" it would be
    dropped on the floor, the watcher would start clean, and suppression would appear
    configured while doing nothing whatsoever.
    """
    path = _write(
        tmp_path,
        {
            "correlation": {
                "default_window_minutes": 5,
                "fingerprint_fields": list(FINGERPRINT_FIELDS),
                "suppresion": [  # typo
                    {
                        "alert_name": "OrderServiceHighMemory",
                        "suppress_follow_on_minutes": 10,
                    }
                ],
            }
        },
    )

    with pytest.raises(ConfigurationError, match="suppresion"):
        load_correlation_rules(path)


def test_a_missing_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="not found"):
        load_correlation_rules(tmp_path / "nope.yaml")


def test_invalid_yaml_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "correlation-rules.yaml"
    path.write_text("correlation: [unclosed\n")

    with pytest.raises(ConfigurationError, match="not valid YAML"):
        load_correlation_rules(path)


@pytest.mark.parametrize(
    ("block", "value"),
    [
        pytest.param("default_window_minutes", 0, id="zero-window"),
        pytest.param("default_window_minutes", -5, id="negative-window"),
    ],
)
def test_a_nonsensical_window_is_refused(
    tmp_path: Path, block: str, value: int
) -> None:
    with pytest.raises(ConfigurationError):
        _load(tmp_path, **{block: value})


def test_a_zero_minute_suppression_cooldown_is_refused(tmp_path: Path) -> None:
    """A zero cooldown suppresses nothing; it is a mistake, not a way to disable."""
    with pytest.raises(ConfigurationError):
        _load(
            tmp_path,
            suppression=[{"alert_name": "X", "suppress_follow_on_minutes": 0}],
        )


def test_an_escalation_threshold_of_one_is_refused(tmp_path: Path) -> None:
    """Threshold 1 would escalate every incident on its first alert.

    That is not correlation, it is a severity rewrite — and it is far more likely to
    be a typo than an intention.
    """
    with pytest.raises(ConfigurationError):
        _load(
            tmp_path,
            escalation=[
                {
                    "alert_count_threshold": 1,
                    "within_minutes": 2,
                    "escalate_to": "critical",
                }
            ],
        )


def test_an_unknown_severity_is_refused(tmp_path: Path) -> None:
    """escalate_to is a closed vocabulary — 'urgent' is not a severity."""
    with pytest.raises(ConfigurationError):
        _load(
            tmp_path,
            escalation=[
                {
                    "alert_count_threshold": 3,
                    "within_minutes": 2,
                    "escalate_to": "urgent",
                }
            ],
        )


def test_duplicate_suppression_rules_are_refused(tmp_path: Path) -> None:
    """The second rule would be unreachable — the lookup returns the first match.

    Refused loudly, rather than letting an operator edit a rule that never applies and
    conclude the feature is broken.
    """
    with pytest.raises(ConfigurationError, match="OrderServiceHighMemory"):
        _load(
            tmp_path,
            suppression=[
                {
                    "alert_name": "OrderServiceHighMemory",
                    "suppress_follow_on_minutes": 10,
                },
                {
                    "alert_name": "OrderServiceHighMemory",
                    "suppress_follow_on_minutes": 20,
                },
            ],
        )


# --- fingerprint_fields: a declaration, enforced -------------------------------


def test_fingerprint_fields_must_match_what_ingestion_hashes(tmp_path: Path) -> None:
    """Editing this list does not change the fingerprint — so it may not disagree.

    Ingestion computes sha256(service_name:alert_name:severity) in code, and the
    watcher trusts that value. If the YAML claims otherwise, the two have drifted and
    every correlation decision rests on a false premise. Refuse to start.
    """
    with pytest.raises(ConfigurationError, match="fingerprint_fields"):
        _load(tmp_path, fingerprint_fields=["service_name", "alert_name"])


def test_fingerprint_fields_order_matters(tmp_path: Path) -> None:
    """The hash is order-dependent, so a reordered declaration is still a lie."""
    with pytest.raises(ConfigurationError):
        _load(
            tmp_path,
            fingerprint_fields=["alert_name", "service_name", "severity"],
        )


# --- the deferred rules, pinned inert (ADR 0013) --------------------------------


def test_deferred_rules_parse_and_validate(tmp_path: Path) -> None:
    """window_overrides and service_groups are still VALIDATED, not ignored.

    They are inert, not unread: a nonsense value in them is still a startup failure,
    so the config cannot rot while it waits for the feature.
    """
    rules = _load(
        tmp_path,
        window_overrides=[{"alert_name": "CrashLoop", "window_minutes": 2}],
        service_groups=[
            {"name": "order-stack", "services": ["order-service", "order-db"]}
        ],
    )

    assert rules.window_overrides[0].window_minutes == 2
    assert rules.service_groups[0].services == ["order-service", "order-db"]

    with pytest.raises(ConfigurationError):
        _load(tmp_path, window_overrides=[{"alert_name": "X", "window_minutes": 0}])


def test_the_rules_expose_no_way_to_apply_the_deferred_ones(tmp_path: Path) -> None:
    """ADR 0013, pinned: correlation reads suppression and escalation. Nothing else.

    The deferred rules are DATA on the model, with no accessor that resolves them —
    there is no `window_for(alert)` and no `group_for(service)`, because a watcher
    cannot honour a window when ingestion has already decided which incident the alert
    landed on. If someone adds one, they must delete this test, and deleting it means
    reading the ADR that explains why merging incidents needs a schema first.
    """
    rules = load_correlation_rules(SHIPPED_RULES)

    assert hasattr(rules, "suppression_for"), "suppression is live"
    assert not hasattr(rules, "window_for"), "windows are ingestion's — see ADR 0013"
    assert not hasattr(rules, "group_for"), "grouping needs a merge lifecycle"

    # The shipped config really does carry the deferred rules — this test would pass
    # vacuously if they had simply been deleted from the YAML.
    assert rules.window_overrides, "the deferred config is present, just not applied"
    assert rules.service_groups


# --- the live lookups ----------------------------------------------------------


def test_suppression_lookup_finds_its_rule_and_only_its_rule() -> None:
    rules = load_correlation_rules(SHIPPED_RULES)

    found = rules.suppression_for("OrderServiceHighMemory")
    assert found is not None
    assert found.suppress_follow_on_minutes == 10

    assert rules.suppression_for("OrderProcessingFailureRate") is None
