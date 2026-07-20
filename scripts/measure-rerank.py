"""Measure the pre-registered probes through the RERANKED pipeline.

The phase's central question — does the most expensive stage earn its keep —
answered against ``tests/retrieval/probes.yaml``'s criterion, which was fixed
before this stage existed:

    FIX   inventory-slow-then-recovering AND
          checkout-customers-leaving-more-than-usual reach best_rank == 1
    HOLD  all 15 probes already at best_rank == 1 stay there

CONFIRMING THE STAGE ACTUALLY RAN
---------------------------------
:class:`GatewayReranker` degrades to the fused ordering on every failure, by
design — an incident should lose reranking's improvement, not its context. That
makes "the probe did not move" ambiguous: it could mean reranking ran and did not
help, or that it never ran at all. Those demand opposite responses, and a
measurement that cannot tell them apart would let a stage that failed 17 times
be recorded as a stage that tried and did not help.

So every call's logs are captured and the outcome read directly:
``knowledge.reranked`` means the scores came back and were applied;
``knowledge.rerank_skipped`` means it degraded, and carries the reason. A probe
whose repeats did not all execute is reported as such and supports no conclusion
about reranking either way.

WHAT IS AND IS NOT RE-MEASURED
------------------------------
Rank is the metric here. The cosine margin is a property of the kNN leg, which
reranking does not touch, so it is unchanged from ``baseline-hybrid.json`` by
construction rather than by measurement — re-recording it would suggest it had
been observed to stay put.
"""

from __future__ import annotations

import asyncio
import json
import pathlib
import subprocess
from collections import Counter
from datetime import UTC, datetime
from typing import Any

import httpx
import structlog
import yaml
from pydantic import SecretStr
from radar_knowledge_service.embeddings import GatewayEmbeddingClient
from radar_knowledge_service.rerank_client import GatewayReranker
from radar_knowledge_service.retrieval import HybridRetriever
from radar_plugin_knowledge_elastic import ElasticKnowledgeStore

ROOT = pathlib.Path(__file__).resolve().parents[1]
PROBES = ROOT / "tests/retrieval/probes.yaml"
HYBRID = ROOT / "tests/retrieval/baseline-hybrid.json"
OUT = ROOT / "tests/retrieval/baseline-reranked.json"
CORPUS = ROOT / "docs/runbooks"
SECRETS = pathlib.Path.home() / ".radar-dev/secrets"

ES = "http://localhost:9200"
INDEX = "radar-runbooks"
GATEWAY = "http://127.0.0.1:8081"
DIMS = 1536
LIMIT = 10
REPEATS = 5


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.strip()


def _service_of(runbook_id: str) -> str:
    frontmatter = yaml.safe_load(
        (CORPUS / f"{runbook_id}.md").read_text().split("---")[1]
    )
    services: list[str] = frontmatter["services"]
    return services[0]


def _best_rank(runbook_ids: list[str], expected: str) -> int | None:
    for position, runbook_id in enumerate(runbook_ids, start=1):
        if runbook_id == expected:
            return position
    return None


async def main() -> None:
    probes = yaml.safe_load(PROBES.read_text())["probes"]
    assert probes, f"no probes parsed from {PROBES} — measuring nothing"
    hybrid = {p["id"]: p for p in json.loads(HYBRID.read_text())["probes"]}
    assert hybrid, "no hybrid baseline to compare against"

    embed_token = (
        (SECRETS / "knowledge-service/gateway_token_embed").read_text().strip()
    )
    reason_token = (
        (SECRETS / "knowledge-service/gateway_token_reason").read_text().strip()
    )

    store = ElasticKnowledgeStore(hosts=ES, dims=DIMS, index=INDEX)
    http = httpx.AsyncClient(base_url=GATEWAY, timeout=None)
    embedder = GatewayEmbeddingClient(http, SecretStr(embed_token), dims=DIMS)
    reranker = GatewayReranker(http, SecretStr(reason_token))
    retriever = HybridRetriever(backend=store, embedder=embedder, reranker=reranker)

    rows: list[dict[str, Any]] = []
    for probe in probes:
        query = " ".join(probe["query"].split())
        expected = probe["expects"]
        service = _service_of(expected)

        ranks: list[int | None] = []
        outcomes: list[str] = []
        for _ in range(REPEATS):
            with structlog.testing.capture_logs() as captured:
                results = await retriever.retrieve(
                    query, service_name=service, limit=LIMIT
                )
            events = [entry.get("event") for entry in captured]
            if "knowledge.reranked" in events:
                outcomes.append("executed")
            else:
                skipped = next(
                    (
                        e
                        for e in captured
                        if e.get("event") == "knowledge.rerank_skipped"
                    ),
                    {},
                )
                outcomes.append(f"degraded:{skipped.get('reason', 'unknown')}")
            ranks.append(_best_rank([h["runbook_id"] for h in results], expected))

        before = hybrid[probe["id"]]["best_rank"]
        rows.append(
            {
                "id": probe["id"],
                "expects": expected,
                "service_name": service,
                "hybrid_rank": before,
                # The pre-registration says a movement on a rank-unstable probe
                # proves nothing, so `fixed` requires stability AND rank 1 —
                # reporting a single repeat as the headline would present
                # [2, 1, 2, 2, 3] as whichever number happened to come last.
                "best_rank": ranks[-1] if len(set(ranks)) == 1 else None,
                "ranks_seen": sorted({r for r in ranks if r is not None}),
                "fixed": len(set(ranks)) == 1 and ranks[-1] == 1,
                "moved": set(ranks) != {before},
                "repeats": {
                    "n": REPEATS,
                    "ranks": ranks,
                    "rank_stable": len(set(ranks)) == 1,
                    "outcomes": outcomes,
                    # The claim this whole measurement rests on: the stage ran.
                    "all_executed": all(o == "executed" for o in outcomes),
                },
            }
        )
        stable = len(set(ranks)) == 1
        # Print what the artifact records. Showing a single repeat with a "fixed"
        # marker would report [3, 1, 1, 3, 1] as "1 *" — the exact false positive
        # this measurement exists to avoid.
        shown = (
            f"{ranks[-1]} *"
            if stable and ranks[-1] == 1
            else (f"{ranks[-1]}  " if stable else f"UNSTABLE {ranks}")
        )
        print(
            f"  {probe['id']:<42} {before} -> {shown}"
            f"  {'executed' if all(o == 'executed' for o in outcomes) else outcomes}"
        )

    await store.close()
    await http.aclose()

    targets = [r for r in rows if r["hybrid_rank"] != 1]
    hold = [r for r in rows if r["hybrid_rank"] == 1]
    verdict = {
        "fix_targets": {r["id"]: r["best_rank"] for r in targets},
        "fix_met": all(r["best_rank"] == 1 for r in targets),
        "hold_count": len(hold),
        "hold_met": all(r["best_rank"] == 1 for r in hold),
        "hold_broken": [r["id"] for r in hold if r["best_rank"] != 1],
        "every_call_executed": all(r["repeats"]["all_executed"] for r in rows),
    }
    verdict["criterion_met"] = bool(verdict["fix_met"] and verdict["hold_met"])

    OUT.write_text(
        json.dumps(
            {
                "measured_at": datetime.now(UTC).isoformat(),
                "commit": _git("rev-parse", "HEAD"),
                "stage": "reranked",
                "description": "BM25 + kNN + RRF, filtered, then cross-encoder rerank",
                "search": {"limit": LIMIT, "leg_size": 20, "fuse_size": 10},
                "rerank": {"mode": "reason", "model": "gpt-4o", "batched": True},
                "criterion": verdict,
                "probes": rows,
            },
            indent=2,
        )
        + "\n"
    )

    print()
    print(f"FIX  targets: {verdict['fix_targets']}  met={verdict['fix_met']}")
    print(
        f"HOLD {verdict['hold_count']} probes  met={verdict['hold_met']}"
        f"  broken={verdict['hold_broken']}"
    )
    print(f"every call executed: {verdict['every_call_executed']}")
    print(f"CRITERION MET: {verdict['criterion_met']}")
    counts = Counter(o for r in rows for o in r["repeats"]["outcomes"])
    print(f"\noutcome counts: {counts}")
    print(f"wrote {OUT.relative_to(ROOT)}")


asyncio.run(main())
