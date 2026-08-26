"""The logs index template + ILM policy, verified against a live Elasticsearch.

Phase 13's shard-economics deliverable is only real if the template actually
governs the per-service indices Fluent Bit creates. This applies the repo's
``deploy/fluent-bit/logs-{ilm-policy,index-template}.json`` to a reachable
Elasticsearch exactly as the ``es-logs-init`` job does, then creates the
``radar-<service>-logs-*`` indices a running Fluent Bit would and proves the
template took: 1 shard, 0 replicas, and the ``radar-logs`` ILM policy attached —
none of which are Elasticsearch defaults, so their presence can only come from the
template. A service-scoped query over ``radar-*-logs-*`` returns only that
service's lines (the read path the logs plugin uses).

Teeth: a control index whose name does NOT match ``radar-*-logs-*`` is created in
the same cluster and gets the plain defaults (1 replica, no lifecycle policy) — so
the assertions above are attributing the settings to the template, not to whatever
Elasticsearch would have done anyway. Break the template's ``index_patterns`` and
the per-service index falls back to those same defaults and the test goes red.

Skips when no Elasticsearch is reachable (``ELASTICSEARCH_URL``, default
``http://localhost:9200``) — it needs a real cluster, not a mock.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_FILE = ROOT / "deploy/fluent-bit/logs-index-template.json"
POLICY_FILE = ROOT / "deploy/fluent-bit/logs-ilm-policy.json"

TEMPLATE_NAME = "radar-logs"
POLICY_NAME = "radar-logs"
# Matches radar-*-logs-* (a Fluent Bit daily per-service index).
ALPHA_INDEX = "radar-alpha-logs-2026.08.25"
BETA_INDEX = "radar-beta-logs-2026.08.25"
# Deliberately does NOT match radar-*-logs-* — the control.
CONTROL_INDEX = "not-a-radar-logs-index-2026.08.25"


def _es_url() -> str:
    return os.environ.get("ELASTICSEARCH_URL", "http://localhost:9200").rstrip("/")


@pytest.fixture
def es() -> Iterator[httpx.Client]:
    url = _es_url()
    try:
        client = httpx.Client(base_url=url, timeout=10)
        client.get("/_cluster/health").raise_for_status()
    except Exception as exc:  # noqa: BLE001 — any connect/HTTP error means "no ES"
        pytest.skip(f"no Elasticsearch at {url}: {type(exc).__name__}")

    _cleanup(client)
    try:
        yield client
    finally:
        _cleanup(client)
        client.close()


def _cleanup(client: httpx.Client) -> None:
    # Template before policy: an ILM policy cannot be deleted while a template
    # references it.
    for index in (ALPHA_INDEX, BETA_INDEX, CONTROL_INDEX):
        client.delete(f"/{index}", params={"ignore_unavailable": "true"})
    client.delete(f"/_index_template/{TEMPLATE_NAME}")
    client.delete(f"/_ilm/policy/{POLICY_NAME}")


def _install(client: httpx.Client) -> None:
    """Apply the policy then the template — the es-logs-init order."""
    policy = json.loads(POLICY_FILE.read_text())
    template = json.loads(TEMPLATE_FILE.read_text())
    client.put(f"/_ilm/policy/{POLICY_NAME}", json=policy).raise_for_status()
    client.put(f"/_index_template/{TEMPLATE_NAME}", json=template).raise_for_status()


def _index_line(client: httpx.Client, index: str, *, service: str, event: str) -> None:
    client.post(
        f"/{index}/_doc",
        params={"refresh": "wait_for"},
        json={"service": service, "event": event, "level": "info"},
    ).raise_for_status()


def _settings(client: httpx.Client, index: str) -> dict[str, Any]:
    body = client.get(f"/{index}/_settings").json()
    settings: dict[str, Any] = body[index]["settings"]["index"]
    return settings


def test_template_governs_per_service_indices(es: httpx.Client) -> None:
    _install(es)

    # A running Fluent Bit would create these on the first log line per service.
    _index_line(es, ALPHA_INDEX, service="alpha", event="a.started")
    _index_line(es, BETA_INDEX, service="beta", event="b.started")

    for index in (ALPHA_INDEX, BETA_INDEX):
        settings = _settings(es, index)
        # Template-only: Elasticsearch's defaults are 1 replica and NO lifecycle.
        assert settings["number_of_shards"] == "1", f"{index}: shard economics lost"
        assert settings["number_of_replicas"] == "0", f"{index}: replica not pinned"
        assert settings.get("lifecycle", {}).get("name") == POLICY_NAME, (
            f"{index}: ILM policy not attached by the template"
        )

    # Read path: a service-scoped query over the shared pattern isolates one service.
    hits = es.post(
        "/radar-*-logs-*/_search",
        json={"query": {"term": {"service": "alpha"}}},
    ).json()["hits"]["hits"]
    assert [h["_source"]["service"] for h in hits] == ["alpha"], (
        "a service-scoped query returned another service's lines"
    )


def test_control_index_outside_the_pattern_gets_defaults(es: httpx.Client) -> None:
    """Teeth: the template is what pins the settings, not the cluster defaults."""
    _install(es)
    _index_line(es, CONTROL_INDEX, service="alpha", event="a.started")

    settings = _settings(es, CONTROL_INDEX)
    # Default replica count is 1 and there is no lifecycle policy — the exact things
    # the template pins for a matching index. If these matched the template, the
    # per-service assertions above would prove nothing.
    assert settings["number_of_replicas"] == "1"
    assert "lifecycle" not in settings
