"""The traces data-stream name is one string shared across three places; pin it.

The join is invisible at runtime and fatal if it drifts:

- the OTel collector exporter (``mapping.mode: otel``, ``deploy/otel/``) WRITES
  the ``traces-generic-default`` data stream,
- ``deploy/otel/traces-index-template.json`` CREATES that data stream, and
- the traces plugin QUERIES it via ``radar_plugin_traces_elastic.TRACES_INDEX``,
  the same constant the Phase-10 step-10 done-condition imports.

If any one of these is renamed alone, spans land in one index and are queried
from another: nothing errors, and "traceable end to end by correlation_id" simply
never finds a span. This test ties the plugin constant to the deploy template so a
rename breaks here instead of silently breaking the pipeline.
"""

from __future__ import annotations

import json
from pathlib import Path

from radar_plugin_traces_elastic import TRACES_INDEX

_ROOT = Path(__file__).resolve().parent.parent
_TEMPLATE = _ROOT / "deploy" / "otel" / "traces-index-template.json"


def test_traces_template_targets_the_plugin_default_index() -> None:
    patterns = json.loads(_TEMPLATE.read_text())["index_patterns"]
    assert patterns == [TRACES_INDEX], (
        f"traces template index_patterns {patterns} must equal the plugin's "
        f"TRACES_INDEX [{TRACES_INDEX!r}] — the exporter, the template, and the "
        f"query must all name the same data stream."
    )
