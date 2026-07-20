"""Measure the pre-registered probes at each retrieval stage boundary.

Writes one file per stage next to the original baseline, rather than replacing
it — the pre-registered instruction in ``tests/retrieval/probes.yaml``. Two
baselines make a stage's contribution READ directly, as the difference between
adjacent files, instead of inferred from a single before/after pair.

STAGES
------
``knn_raw``        kNN, no pre-filter. Already recorded in ``baseline.json``;
                   re-measured here so all three stages share one run's
                   embeddings and are therefore comparable.
``knn_filtered``   kNN with the ``services`` pre-filter. The difference from
                   ``knn_raw`` is the FILTER bucket, and nothing else changes.
``hybrid``         BM25 + kNN + RRF, filtered. The difference from
                   ``knn_filtered`` is the HYBRID bucket.

THE HYBRID STAGE RUNS THE PRODUCTION RETRIEVER
----------------------------------------------
It calls :class:`HybridRetriever` — the same object the service uses — rather
than reimplementing embed/search/fuse here. A measurement that reimplements the
thing it measures tells you the reimplementation works. The probes are plain
strings and ``retrieve`` takes a plain string, which is exactly why the query
assembly was split into its own layer.

METRICS, AS PRE-REGISTERED
--------------------------
- ``margin`` — cosine, on the kNN LEG only. Survives into the hybrid stage
  unchanged because RRF does not touch the leg; it stays comparable to
  ``baseline.json``.
- ``best_rank`` — position of the expected runbook's best chunk in the stage's
  OUTPUT. For hybrid that is the fused ordering, where no score margin exists.
- ``fixed`` — ``best_rank == 1``. Defined before any of these numbers existed.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
from datetime import UTC, datetime
from typing import Any

import httpx
import yaml
from pydantic import SecretStr
from radar_knowledge_service.embeddings import GatewayEmbeddingClient
from radar_knowledge_service.retrieval import HybridRetriever
from radar_plugin_knowledge_elastic import ElasticKnowledgeStore

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBES = ROOT / "tests/retrieval/probes.yaml"
OUT_DIR = ROOT / "tests/retrieval"
CORPUS = ROOT / "docs/runbooks"
SECRETS = pathlib.Path.home() / ".radar-dev/secrets"

ES = "http://localhost:9200"
INDEX = "radar-runbooks"
GATEWAY = "http://127.0.0.1:8081"
DIMS = 1536
K = 10
#: Raised from 5 after reranking was shown to vary between identical
#: inputs, and five repeats produced a confident false positive on the
#: headline metric. Declared in tests/retrieval/probes.yaml before
#: re-measuring. Applied uniformly to every probe and every stage.
REPEATS = 20


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _service_of(runbook_id: str) -> str:
    """The service an incident for this runbook would carry.

    Derived from the corpus rather than duplicated into probes.yaml: it is a
    property of the runbook, and a second copy could disagree with the first.
    """
    frontmatter = yaml.safe_load(
        (CORPUS / f"{runbook_id}.md").read_text().split("---")[1]
    )
    services: list[str] = frontmatter["services"]
    assert services, f"{runbook_id} declares no services"
    return services[0]


def _best_rank(runbook_ids: list[str], expected: str) -> int | None:
    for position, runbook_id in enumerate(runbook_ids, start=1):
        if runbook_id == expected:
            return position
    return None


def _margin(hits: list[dict[str, Any]], expected: str) -> float | None:
    """Cosine margin on a kNN hit list: expected's best minus the best other."""
    correct = next((h for h in hits if h["runbook_id"] == expected), None)
    wrong = next((h for h in hits if h["runbook_id"] != expected), None)
    if correct is None or wrong is None:
        return None
    return float(correct["score"]) - float(wrong["score"])


def _summarise(margins: list[float | None], ranks: list[int | None]) -> dict[str, Any]:
    measured = [m for m in margins if m is not None]
    return {
        "margin": round(measured[-1], 6) if measured else None,
        "best_rank": ranks[-1],
        "fixed": ranks[-1] == 1,
        "in_reasoner_top5": ranks[-1] is not None and ranks[-1] <= 5,
        "repeats": {
            "n": REPEATS,
            "margins": [None if m is None else round(m, 6) for m in margins],
            "spread": round(max(measured) - min(measured), 6) if measured else None,
            "ranks": ranks,
            "rank_stable": len(set(ranks)) == 1,
            "sign_unstable": len({m > 0 for m in measured}) > 1 if measured else False,
        },
    }


async def main() -> None:
    probes = yaml.safe_load(PROBES.read_text())["probes"]
    assert probes, f"no probes parsed from {PROBES} — measuring nothing"

    token = (SECRETS / "knowledge-service/gateway_token_embed").read_text().strip()
    store = ElasticKnowledgeStore(hosts=ES, dims=DIMS, index=INDEX)
    http = httpx.AsyncClient(base_url=GATEWAY, timeout=None)
    embedder = GatewayEmbeddingClient(http, SecretStr(token), dims=DIMS)
    retriever = HybridRetriever(backend=store, embedder=embedder)

    async with httpx.AsyncClient(timeout=None) as raw:
        count = (await raw.get(f"{ES}/{INDEX}/_count")).json()["count"]
    assert count, f"{INDEX} is empty — nothing to measure against"

    stages: dict[str, list[dict[str, Any]]] = {
        "knn_raw": [],
        "knn_filtered": [],
        "hybrid": [],
    }

    for probe in probes:
        query = " ".join(probe["query"].split())
        expected = probe["expects"]
        service = _service_of(expected)

        collected: dict[str, tuple[list[Any], list[Any]]] = {
            name: ([], []) for name in stages
        }

        for _ in range(REPEATS):
            (vector,) = await embedder.embed([query])

            raw_hits = await store.search_knn(vector, size=K, num_candidates=count)
            collected["knn_raw"][0].append(_margin(raw_hits, expected))
            collected["knn_raw"][1].append(
                _best_rank([h["runbook_id"] for h in raw_hits], expected)
            )

            filtered = await store.search_knn(
                vector, service_name=service, size=K, num_candidates=count
            )
            collected["knn_filtered"][0].append(_margin(filtered, expected))
            collected["knn_filtered"][1].append(
                _best_rank([h["runbook_id"] for h in filtered], expected)
            )

            fused = await retriever.retrieve(query, service_name=service, limit=K)
            # Margin is the kNN leg's, unchanged by fusion — the pre-registered
            # continuity with baseline.json. Rank is the fused ordering's.
            collected["hybrid"][0].append(_margin(filtered, expected))
            collected["hybrid"][1].append(
                _best_rank([h["runbook_id"] for h in fused], expected)
            )

        for name in stages:
            margins, ranks = collected[name]
            stages[name].append(
                {
                    "id": probe["id"],
                    "expects": expected,
                    "service_name": service,
                    **_summarise(margins, ranks),
                }
            )

    await store.close()
    await http.aclose()

    common = {
        "measured_at": datetime.now(UTC).isoformat(),
        "commit": _git("rev-parse", "HEAD"),
        "index": {"name": INDEX, "docs": count, "quantization": "int8_hnsw"},
        "embedding": {"model": "text-embedding-3-small", "dims": DIMS},
    }
    descriptions = {
        "knn_raw": "kNN only, no services pre-filter (matches baseline.json)",
        "knn_filtered": "kNN with the services pre-filter — FILTER bucket",
        "hybrid": "BM25 + kNN fused with RRF, filtered — HYBRID bucket",
    }
    for name, rows in stages.items():
        path = OUT_DIR / f"baseline-{name.replace('_', '-')}.json"
        path.write_text(
            json.dumps(
                {
                    **common,
                    "stage": name,
                    "description": descriptions[name],
                    "search": {"k": K, "leg_size": 20, "num_candidates": count},
                    "probes": rows,
                },
                indent=2,
            )
            + "\n"
        )

    print(f"{'probe':<32} {'raw':>12} {'filtered':>12} {'hybrid':>12}")
    for i, probe in enumerate(probes):
        cells = []
        for name in ("knn_raw", "knn_filtered", "hybrid"):
            row = stages[name][i]
            rank = row["best_rank"]
            cells.append(
                f"{'-' if rank is None else rank}{'*' if row['fixed'] else ''}"
            )
        print(f"{probe['id']:<32} {cells[0]:>12} {cells[1]:>12} {cells[2]:>12}")
    print("\nrank per stage; * = fixed (rank 1)")


asyncio.run(main())
