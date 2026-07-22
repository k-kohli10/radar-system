"""Measure the pre-registered retrieval probes and record the baseline.

Reads ``tests/retrieval/probes.yaml``, runs each query as a kNN search against
the live index, and writes ``tests/retrieval/baseline.json``.

THE METRIC: score margin
------------------------
``margin = (best score among chunks of the expected runbook)
          - (best score among chunks of any OTHER runbook)``

Positive is a hit, negative is a miss, and the MAGNITUDE is the point: -0.004 is
a coin flip that anything could tip, -0.033 is a real gap that needs a real
mechanism to close. Rank alone ("was it first?") would throw that away and make a
near-miss indistinguishable from a rout.

WHY num_candidates COVERS THE WHOLE CORPUS
------------------------------------------
``int8_hnsw`` is approximate on two independent axes: int8 quantization of the
vectors, and HNSW graph traversal visiting only part of the graph. This baseline
exists to characterise the index as it will actually be run, so quantization
stays. Traversal, however, is a confound we can simply remove: setting
``num_candidates`` to the full corpus size makes the search explore everything
and return exact-for-this-index results. Any difference from a full-precision
score is then attributable to quantization, not to which nodes the graph happened
to visit — which matters most for the probes whose margins are near zero, where
traversal noise alone could flip the sign.

Raw kNN, no ``services`` pre-filter: the probes are designed to test whether the
vectors separate confusable runbooks WITHIN a service, and the pre-filter cannot
help there. Filtering would make the task easier than the one being measured.

THE NOISE FLOOR, AND WHY IT IS MEASURED RATHER THAN ASSUMED
-----------------------------------------------------------
A third approximation axis turned up while validating this script, and it is not
one of the two the index is usually blamed for: **the embedding API does not
return bit-identical vectors for identical input** (observed max component delta
~9e-5). Elasticsearch is exactly deterministic given a fixed vector — repeated
searches agree to the last digit — so every bit of run-to-run wobble enters
upstream, from the provider.

That is why each probe is measured ``REPEATS`` times and the spread recorded. A
margin is only meaningful against that spread: a later claim that reranking
"moved" a probe has to beat the wobble, and for the near-zero probes the wobble
is within an order of magnitude of the margin itself. Recording a single number
per probe would have hidden this entirely.
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

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBES = ROOT / "tests/retrieval/probes.yaml"
BASELINE = ROOT / "tests/retrieval/baseline.json"
SECRETS = pathlib.Path.home() / ".radar-dev/secrets"

ES = "http://localhost:9200"
INDEX = "radar-runbooks"
GATEWAY = "http://127.0.0.1:8081"
DIMS = 1536
K = 10
#: Repeats per probe. The embedding provider is the only nondeterministic
#: component; this is what turns its wobble into a recorded number.
REPEATS = 5


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


async def main() -> None:
    probes = yaml.safe_load(PROBES.read_text())["probes"]
    assert probes, f"no probes parsed from {PROBES} — measuring nothing"

    token = (SECRETS / "knowledge-service/gateway_token_embed").read_text().strip()

    async with httpx.AsyncClient(timeout=None) as es:
        count = (await es.get(f"{ES}/{INDEX}/_count")).json()["count"]
        assert count, f"{INDEX} is empty — nothing to measure against"
        mapping = (await es.get(f"{ES}/{INDEX}/_mapping")).json()
        vector_field = mapping[INDEX]["mappings"]["properties"]["embedding"]

        # Explore the entire corpus, so traversal is exact and quantization is
        # the only approximation left in the measurement.
        num_candidates = count

        http = httpx.AsyncClient(base_url=GATEWAY, timeout=None)
        embedder = GatewayEmbeddingClient(http, SecretStr(token), dims=DIMS)

        async def search(query: str) -> list[dict[str, Any]]:
            (vector,) = await embedder.embed([query])
            response = await es.post(
                f"{ES}/{INDEX}/_search",
                json={
                    "knn": {
                        "field": "embedding",
                        "query_vector": vector,
                        "k": K,
                        "num_candidates": num_candidates,
                    },
                    "_source": ["runbook_id", "section", "chunk_id"],
                    "size": K,
                },
            )
            hits: list[dict[str, Any]] = response.json()["hits"]["hits"]
            assert hits, "search returned no hits"
            return hits

        def margin_of(hits: list[dict[str, Any]], expected: str) -> float | None:
            correct = next(
                (h for h in hits if h["_source"]["runbook_id"] == expected), None
            )
            wrong = next(
                (h for h in hits if h["_source"]["runbook_id"] != expected), None
            )
            assert wrong is not None, (
                f"every hit in the top {K} is {expected}, so there is no "
                f"competing chunk to measure a margin against. Raise K."
            )
            return None if correct is None else correct["_score"] - wrong["_score"]

        def best_rank_of(hits: list[dict[str, Any]], expected: str) -> int | None:
            """1-indexed position of the expected runbook's best chunk.

            The rank metric, and the one that survives RRF: fusion reorders by
            rank and discards scores, so once the hybrid slice lands this is the
            only one of the two quantities the pipeline still produces.
            """
            for position, hit in enumerate(hits, start=1):
                if hit["_source"]["runbook_id"] == expected:
                    return position
            return None

        results: list[dict[str, Any]] = []
        for probe in probes:
            query = " ".join(probe["query"].split())
            expected = probe["expects"]

            # Repeats first: the spread is what makes the headline margin
            # interpretable, so it is not optional extra detail. Ranks are
            # collected from the SAME searches — a rank is only comparable to the
            # margin that produced it.
            repeat_hits = [await search(query) for _ in range(REPEATS)]
            repeats = [margin_of(h, expected) for h in repeat_hits]
            ranks = [best_rank_of(h, expected) for h in repeat_hits]
            measured = [m for m in repeats if m is not None]
            spread = round(max(measured) - min(measured), 6) if measured else None
            flips = len({m > 0 for m in measured}) > 1
            # Rank needs its own stability floor. The margin spread is a
            # statement about scores; it says nothing about whether a ~1e-4
            # wobble is enough to swap two near-tied chunks and move a rank.
            rank_stable = len(set(ranks)) == 1

            hits = await search(query)
            correct = next(
                (h for h in hits if h["_source"]["runbook_id"] == expected), None
            )
            wrong = next(
                (h for h in hits if h["_source"]["runbook_id"] != expected), None
            )
            assert wrong is not None, (
                f"probe {probe['id']}: every hit in the top {K} is the expected "
                f"runbook, so there is no competing chunk to measure a margin "
                f"against. Raise K."
            )

            # A correct chunk outside the top K cannot be scored from this
            # response. Record it as such rather than inventing a number.
            margin = (
                round(correct["_score"] - wrong["_score"], 6)
                if correct is not None
                else None
            )
            best_rank = best_rank_of(hits, expected)

            results.append(
                {
                    "id": probe["id"],
                    "query": query,
                    "expects": expected,
                    "outcome": (
                        "not_in_top_k"
                        if margin is None
                        else ("hit" if margin > 0 else "miss")
                    ),
                    "margin": margin,
                    # The rank metric. `fixed` is the pre-registered success
                    # criterion — see probes.yaml for its exact definition and
                    # why rank 1 rather than a softer bar.
                    "best_rank": best_rank,
                    "fixed": best_rank == 1,
                    "in_reasoner_top5": best_rank is not None and best_rank <= 5,
                    "repeats": {
                        "n": REPEATS,
                        "margins": [
                            None if m is None else round(m, 6) for m in repeats
                        ],
                        "spread": spread,
                        # True means repeated measurement of the SAME query
                        # disagreed about hit vs miss. Such a probe cannot
                        # support any claim about reranking either way.
                        "sign_unstable": flips,
                        "ranks": ranks,
                        "rank_stable": rank_stable,
                    },
                    "expected_best": (
                        {
                            "score": round(correct["_score"], 6),
                            "section": correct["_source"]["section"],
                            "chunk_id": correct["_source"]["chunk_id"],
                        }
                        if correct is not None
                        else None
                    ),
                    "winning_other": {
                        "runbook_id": wrong["_source"]["runbook_id"],
                        "score": round(wrong["_score"], 6),
                        "section": wrong["_source"]["section"],
                        "chunk_id": wrong["_source"]["chunk_id"],
                    },
                    "top_runbooks": [h["_source"]["runbook_id"] for h in hits],
                }
            )

        await http.aclose()

    baseline = {
        "measured_at": datetime.now(UTC).isoformat(),
        "commit": _git("rev-parse", "HEAD"),
        "branch": _git("rev-parse", "--abbrev-ref", "HEAD"),
        "index": {
            "name": INDEX,
            "docs": count,
            "vector_field": vector_field,
            "quantization": vector_field.get("index_options", {}).get("type"),
        },
        "search": {
            "type": "knn",
            "k": K,
            "num_candidates": num_candidates,
            "services_prefilter": False,
        },
        "embedding": {"model": "text-embedding-3-small", "dims": DIMS},
        "metric": "expected_best_score - winning_other_best_score",
        "probes": results,
    }
    BASELINE.write_text(json.dumps(baseline, indent=2) + "\n")

    header = (
        f"{'probe':<32} {'outcome':<8} {'margin':>10} {'spread':>9} "
        f"{'rank':>5} {'fixed':>6}  rank-stable"
    )
    print(header)
    for row in results:
        margin = "n/a" if row["margin"] is None else f"{row['margin']:+.6f}"
        spread = row["repeats"]["spread"]
        spread_s = "n/a" if spread is None else f"{spread:.6f}"
        rank = row["best_rank"]
        print(
            f"{row['id']:<32} {row['outcome']:<8} {margin:>10} {spread_s:>9} "
            f"{'-' if rank is None else rank:>5} "
            f"{'yes' if row['fixed'] else 'no':>6}  "
            f"{'yes' if row['repeats']['rank_stable'] else 'NO'}"
        )
    print(f"\nwrote {BASELINE.relative_to(ROOT)}")


asyncio.run(main())
