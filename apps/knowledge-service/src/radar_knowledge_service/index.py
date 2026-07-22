"""One indexing pass, runnable.

``make index``, or ``python -m radar_knowledge_service.index``.

Composes the proven parts — the store, the embedding client, the incremental
indexer — from the same settings and Vault-mounted secrets the service uses.
Failures are loud: a missing secret, an unreachable cluster, or an embedding
failure raises and exits non-zero, because a cron job that swallows its own
failure is an index that quietly stops updating.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import httpx
from radar_common import bootstrap, read_secret

from radar_knowledge_service.config import KnowledgeSettings, load_gateway_tokens
from radar_knowledge_service.embeddings import GatewayEmbeddingClient
from radar_knowledge_service.indexer import IndexRunResult, RunbookIndexer

CORPUS_DIR = Path(__file__).resolve().parents[4] / "docs" / "runbooks"
"""The repo corpus. In production this is the mounted runbook volume."""


async def run_once(settings: KnowledgeSettings, corpus_dir: Path) -> IndexRunResult:
    """Build the pipeline from config + secrets and run one pass."""
    from radar_database import Database
    from radar_plugin_knowledge_elastic import ElasticKnowledgeStore

    dsn = read_secret("postgres_dsn")
    assert dsn is not None  # required=True: raised if absent
    embed_token, _ = load_gateway_tokens()

    database = Database(dsn.get_secret_value())
    store = ElasticKnowledgeStore(
        hosts=settings.elasticsearch_url,
        dims=settings.embedding_dims,
        index=settings.index_name,
    )
    http = httpx.AsyncClient(base_url=settings.gateway_url, timeout=None)
    try:
        indexer = RunbookIndexer(
            index=store,
            embedder=GatewayEmbeddingClient(
                http, embed_token, dims=settings.embedding_dims
            ),
            session_factory=database.session_factory,
            corpus_dir=corpus_dir,
        )
        return await indexer.run()
    finally:
        await store.close()
        await http.aclose()
        await database.dispose()


def main() -> None:
    runtime = bootstrap(KnowledgeSettings, with_agent_auth=False)
    settings = runtime.settings
    if not CORPUS_DIR.is_dir():
        sys.exit(f"ERROR: corpus directory {CORPUS_DIR} does not exist")

    result = asyncio.run(run_once(settings, CORPUS_DIR))
    print(
        f"indexed: embedded={result.embedded} indexed={result.indexed} "
        f"deleted={result.deleted} processed={result.processed} "
        f"skipped={result.skipped} removed={result.removed}"
        + ("  (no-op: corpus unchanged)" if result.is_noop else "")
    )


if __name__ == "__main__":
    main()
