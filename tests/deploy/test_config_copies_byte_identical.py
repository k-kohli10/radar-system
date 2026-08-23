"""Every config file that exists in two places MUST stay byte-identical.

Some configs live twice by necessity: compose mounts a standalone file, while a
static k8s manifest needs the same content inline in a ConfigMap. Nothing
structural keeps the two copies in step — Phase 10 kept them identical by hand.
This pins the whole CLASS of that drift (deferred from Phase 10): for each
registered pair, extract the ConfigMap's embedded value and assert it equals the
standalone file verbatim.

Covered pairs:
  - deploy/otel/collector-config.yaml        <-> otel-collector-config ConfigMap
  - deploy/otel/traces-index-template.json   <-> traces-index-template ConfigMap
  - deploy/fluent-bit/parsers.conf           <-> fluent-bit-config ConfigMap
  - every deploy/grafana/dashboards/*.json   <-> its grafana-dashboard-* ConfigMap
    (discovered dynamically, so a newly added dashboard is covered automatically)

DELIBERATELY NOT covered: the fluent-bit-config ConfigMap's ``fluent-bit.conf``
key. The two fluent-bit.conf files legitimately DIFFER — compose tails
``.dev-run`` while k8s tails container logs — so only ``parsers.conf`` is a
byte-identical pair. Adding fluent-bit.conf here would be wrong.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]

# (configmap manifest, data key, standalone file) — explicit because the
# standalone path is not derivable from the key. A deliberate touch-point: add a
# pair here on purpose when a config gains a second home.
EXPLICIT_PAIRS: list[tuple[str, str, str]] = [
    (
        "deploy/otel/collector-daemonset.yaml",
        "collector-config.yaml",
        "deploy/otel/collector-config.yaml",
    ),
    (
        "deploy/otel/traces-index-template.yaml",
        "traces-index-template.json",
        "deploy/otel/traces-index-template.json",
    ),
    (
        "deploy/fluent-bit/fluent-bit-daemonset.yaml",
        "parsers.conf",
        "deploy/fluent-bit/parsers.conf",
    ),
]

GRAFANA_CONFIGMAPS = "deploy/grafana/dashboards-configmaps.yaml"
GRAFANA_DASHBOARDS = "deploy/grafana/dashboards"

# Plain file <-> file copies. Helm's .Files.Get can only read files inside the
# chart directory, so the compose vault-init script cannot be shared by reference;
# the chart keeps a copy that its ConfigMap embeds. This pins the two copies.
FILE_PAIRS: list[tuple[str, str]] = [
    (
        "deploy/compose/vault-init/fetch-secrets.sh",
        "deploy/helm/radar/files/fetch-secrets.sh",
    ),
    (
        "apps/watcher-agent/config/correlation-rules.yaml",
        "deploy/helm/radar/files/correlation-rules.yaml",
    ),
    (
        "apps/planner-agent/config/plan-templates.yaml",
        "deploy/helm/radar/files/plan-templates.yaml",
    ),
    (
        "apps/llm-gateway/config/gateway.yaml",
        "deploy/helm/radar/files/gateway.yaml",
    ),
    (
        "deploy/prometheus/alerting-rules.yml",
        "deploy/helm/platform-deps/files/alerting-rules.yml",
    ),
    (
        "deploy/prometheus/radar-service-alerts.yml",
        "deploy/helm/platform-deps/files/radar-service-alerts.yml",
    ),
    (
        "deploy/grafana/provisioning/datasources/prometheus.yml",
        "deploy/helm/platform-deps/files/grafana/datasources/prometheus.yml",
    ),
    (
        "deploy/grafana/provisioning/dashboards/radar.yml",
        "deploy/helm/platform-deps/files/grafana/dashboards-provider.yml",
    ),
]


def _grafana_dashboard_file_pairs() -> list[tuple[str, str]]:
    """Each Grafana dashboard JSON, paired with its platform-deps chart copy.

    Discovered from disk so a newly added dashboard is covered automatically.
    """
    src_dir = ROOT / "deploy/grafana/dashboards"
    copy_dir = ROOT / "deploy/helm/platform-deps/files/grafana/dashboards"
    pairs: list[tuple[str, str]] = []
    for src in sorted(src_dir.glob("*.json")):
        pairs.append(
            (
                f"deploy/grafana/dashboards/{src.name}",
                f"deploy/helm/platform-deps/files/grafana/dashboards/{src.name}",
            )
        )
    assert copy_dir.is_dir(), "platform-deps grafana dashboards copy dir missing"
    return pairs


def _all_file_pairs() -> list[tuple[str, str]]:
    return FILE_PAIRS + _grafana_dashboard_file_pairs()


def _configmaps(manifest: str) -> list[dict[str, Any]]:
    docs = yaml.safe_load_all((ROOT / manifest).read_text())
    return [d for d in docs if isinstance(d, dict) and d.get("kind") == "ConfigMap"]


def _grafana_pairs() -> list[tuple[str, str, str]]:
    """Discover one pair per dashboard embedded in the Grafana ConfigMaps."""
    pairs: list[tuple[str, str, str]] = []
    for cm in _configmaps(GRAFANA_CONFIGMAPS):
        for key in cm.get("data", {}):
            pairs.append((GRAFANA_CONFIGMAPS, key, f"{GRAFANA_DASHBOARDS}/{key}"))
    return pairs


def _all_pairs() -> list[tuple[str, str, str]]:
    return EXPLICIT_PAIRS + _grafana_pairs()


def _embedded_value(manifest: str, key: str) -> str:
    for cm in _configmaps(manifest):
        data = cm.get("data", {})
        if key in data:
            value = data[key]
            assert isinstance(value, str)
            return value
    raise AssertionError(f"no ConfigMap in {manifest} carries data key {key!r}")


def test_pair_discovery_is_not_vacuous() -> None:
    """Guard: an empty/broken discovery must not let the suite pass green."""
    pairs = _all_pairs()
    # 3 explicit + the five Phase 10 dashboards. A floor, so new dashboards are
    # welcome but a discovery that silently finds nothing fails loudly.
    assert len(_grafana_pairs()) >= 5, "Grafana dashboard pairs vanished"
    assert len(pairs) >= 8, f"expected >=8 config copy-pairs, found {len(pairs)}"
    assert len(FILE_PAIRS) >= 8, "file copy-pairs vanished"
    assert len(_grafana_dashboard_file_pairs()) >= 5, "grafana dashboard pairs vanished"


@pytest.mark.parametrize(
    ("standalone", "copy"),
    _all_file_pairs(),
    ids=lambda v: v.rsplit("/", 2)[-1] if isinstance(v, str) else v,
)
def test_file_copy_is_byte_identical(standalone: str, copy: str) -> None:
    standalone_path = ROOT / standalone
    copy_path = ROOT / copy
    assert standalone_path.is_file(), f"standalone file missing: {standalone}"
    assert copy_path.is_file(), f"copy missing: {copy}"
    assert copy_path.read_text() == standalone_path.read_text(), (
        f"DRIFT: {standalone} and {copy} are no longer byte-identical. "
        f"Re-sync one from the other."
    )


@pytest.mark.parametrize(
    ("manifest", "key", "standalone"),
    _all_pairs(),
    ids=lambda v: v.rsplit("/", 1)[-1] if isinstance(v, str) else v,
)
def test_configmap_copy_is_byte_identical(
    manifest: str, key: str, standalone: str
) -> None:
    standalone_path = ROOT / standalone
    assert standalone_path.is_file(), f"standalone file missing: {standalone}"
    embedded = _embedded_value(manifest, key)
    on_disk = standalone_path.read_text()
    assert embedded == on_disk, (
        f"DRIFT: {standalone} and its embedded copy ({key} in {manifest}) "
        f"are no longer byte-identical. Re-sync the ConfigMap from the standalone "
        f"file (or vice versa)."
    )
