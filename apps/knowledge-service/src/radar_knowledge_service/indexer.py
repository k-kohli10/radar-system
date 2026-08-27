"""The indexing run: the I/O shell over a proven core.

Everything difficult about incremental indexing was decided in
:mod:`radar_knowledge_service.reconciliation`, which is pure and exhaustively
tested. This module does no deciding. It reads files, asks reconciliation what
to do, and performs it in an order chosen so that any failure leaves the system
in a state the next run can finish from.

TWO ORDERING RULES, AND WHY EACH DIRECTION WAS CHOSEN
------------------------------------------------------
**Elasticsearch first, the Postgres manifest last.** The manifest records "this
file's content is indexed". Written before the chunks reached Elasticsearch, a
crash would leave the next run comparing hashes, seeing no change, and skipping
the file permanently and silently with its chunks absent from the index. Writing
it last means a crash leaves the manifest stale and the next run redoes the work.

**New chunks are indexed BEFORE stale ones are deleted.** The reverse order has
a window where a section's old chunk is gone and its replacement has not landed;
a crash inside that window makes the content unretrievable. Deleting last leaves
outdated-but-present content instead, which is the better wrong state to fail
into: retrieval still grounds the RCA in something, and the next run corrects it.

FAILURES PROPAGATE
------------------
Per :mod:`radar_knowledge_service.embeddings`, embedding failures raise. Nothing
here catches them: the manifest is not advanced for the runbook that failed, and
the index keeps whatever it already had. Partial progress from earlier runbooks
IS committed, because their content is genuinely indexed.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from radar_common import get_logger, utcnow
from radar_database import RunbookDocument
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from radar_knowledge_service.chunking import Chunk, chunk_runbook, compute_document_hash
from radar_knowledge_service.reconciliation import diff_corpus, diff_runbook

log = get_logger("knowledge.indexer")


class KnowledgeIndex(Protocol):
    """The write side of the chunk index, as the indexer needs it.

    A Protocol rather than the concrete Elasticsearch class so this module holds
    no vendor dependency; the plugin satisfies it structurally.
    """

    async def ensure_index(self) -> bool: ...

    async def verify_dims(self) -> int: ...

    async def index(
        self, documents: list[dict[str, Any]], *, refresh: bool = False
    ) -> int: ...

    async def chunk_ids_for(self, runbook_id: str) -> set[str]: ...

    async def delete_chunks(
        self, chunk_ids: set[str], *, refresh: bool = False
    ) -> int: ...


class Embedder(Protocol):
    """Just the part of the gateway client the indexer uses."""

    @property
    def dims(self) -> int: ...

    def check_budget(self, texts: list[str]) -> None: ...

    async def embed(self, texts: list[str]) -> list[list[float]]: ...


@dataclass(frozen=True)
class IndexRunResult:
    """What a run actually did, counted, because the counts are the guarantee.

    ``embedded`` is the number that matters: it is the gateway calls a run cost,
    and it must be zero when nothing changed. A correct index says nothing about
    whether the run was incremental; this does.
    """

    embedded: int = 0
    indexed: int = 0
    deleted: int = 0
    processed: int = 0
    skipped: int = 0
    removed: int = 0

    @property
    def is_noop(self) -> bool:
        return self.embedded == 0 and self.indexed == 0 and self.deleted == 0


class DimensionMismatchError(RuntimeError):
    """The live index and the embedding model disagree about vector size.

    Raised before any work, because every document would be rejected anyway and
    the real fix is a new index plus a re-index, not a retry.
    """


def chunk_to_document(
    chunk: Chunk, embedding: list[float], indexed_at: datetime
) -> dict[str, Any]:
    """Pair one chunk with its vector, in the shape the index mapping expects.

    ``indexed_at`` is passed in rather than computed here so every chunk written
    by one run shares a single timestamp. That makes "which chunks did this run
    touch" an exact query instead of a range that has to guess at clock skew
    between documents.

    It is not part of ``chunk_id`` (a content hash), so it can never trigger a
    re-embed. See ``build_mapping`` in the Elasticsearch plugin for why the field
    exists at all.
    """
    return {
        "chunk_id": chunk.chunk_id,
        "runbook_id": chunk.runbook_id,
        "title": chunk.title,
        "section": chunk.section,
        "text": chunk.text,
        "embedding": embedding,
        "services": list(chunk.services),
        "severity": chunk.severity,
        # The corpus is `fixture` until a human review pass. Carried through to
        # the reasoner so an RCA grounded in unreviewed content says so, rather
        # than reading as though a reviewed runbook backed it.
        "status": chunk.status,
        "alert_name": chunk.alert_name,
        "ordinal": chunk.ordinal,
        "indexed_at": indexed_at.isoformat(),
    }


def read_corpus(directory: Path) -> dict[str, tuple[Path, str]]:
    """Read every runbook, keyed by id, with its source.

    ``README.md`` documents the corpus and is not part of it. The runbook id is
    the filename stem, the join the contract test enforces.
    """
    return {
        path.stem: (path, path.read_text())
        for path in sorted(directory.glob("*.md"))
        if path.name != "README.md"
    }


class RunbookIndexer:
    """Indexes the runbook corpus incrementally."""

    def __init__(
        self,
        *,
        index: KnowledgeIndex,
        embedder: Embedder,
        session_factory: async_sessionmaker[AsyncSession],
        corpus_dir: Path,
    ) -> None:
        self._index = index
        self._embedder = embedder
        self._sessions = session_factory
        self._corpus_dir = corpus_dir

    async def run(self) -> IndexRunResult:
        """Bring the index in line with the corpus on disk."""
        await self._index.ensure_index()
        await self._assert_dimensions_match()

        corpus = read_corpus(self._corpus_dir)
        on_disk = {
            runbook_id: compute_document_hash(source)
            for runbook_id, (_, source) in corpus.items()
        }
        manifest = await self._load_manifest()

        corpus_diff = diff_corpus(on_disk=on_disk, manifest=manifest)
        log.info(
            "knowledge.index_run_planned",
            changed=len(corpus_diff.changed),
            unchanged=len(corpus_diff.unchanged),
            removed=len(corpus_diff.removed),
        )

        result = IndexRunResult(skipped=len(corpus_diff.unchanged))

        # One timestamp for the whole run, taken before any writing, so every
        # chunk this run writes carries it whichever runbook it came from. That
        # makes "which chunks did run N write" a single-term query, which
        # `runbook_documents.indexed_at` cannot answer.
        written_at = utcnow()

        for runbook_id in corpus_diff.removed:
            result = await self._remove_runbook(runbook_id, result)

        for runbook_id in corpus_diff.changed:
            _, source = corpus[runbook_id]
            result = await self._index_runbook(
                runbook_id, source, on_disk[runbook_id], result, written_at
            )

        log.info(
            "knowledge.index_run_complete",
            embedded=result.embedded,
            indexed=result.indexed,
            deleted=result.deleted,
            skipped=result.skipped,
            removed=result.removed,
        )
        return result

    async def _assert_dimensions_match(self) -> None:
        live = await self._index.verify_dims()
        if live != self._embedder.dims:
            raise DimensionMismatchError(
                f"the index stores {live}-dimension vectors but the embedding "
                f"model returns {self._embedder.dims}. This needs a new index and "
                f"a full re-index, not a retry."
            )

    async def _load_manifest(self) -> dict[str, str]:
        async with self._sessions() as session:
            rows = await session.execute(
                select(RunbookDocument.runbook_id, RunbookDocument.content_hash)
            )
            return {runbook_id: digest for runbook_id, digest in rows.all()}

    async def _index_runbook(
        self,
        runbook_id: str,
        source: str,
        document_hash: str,
        result: IndexRunResult,
        written_at: datetime,
    ) -> IndexRunResult:
        chunks = chunk_runbook(source)

        # The budget assertion the chunker deliberately defers: the chunker is
        # pure and knows nothing of a model's limits; this layer does.
        self._embedder.check_budget([chunk.text for chunk in chunks])

        stored = await self._index.chunk_ids_for(runbook_id)
        diff = diff_runbook(runbook_id, chunks, stored)

        embedded = 0
        indexed = 0
        if diff.to_embed:
            vectors = await self._embedder.embed([c.text for c in diff.to_embed])
            documents = [
                chunk_to_document(chunk, vector, written_at)
                for chunk, vector in zip(diff.to_embed, vectors, strict=True)
            ]
            indexed = await self._index.index(documents, refresh=True)
            embedded = len(diff.to_embed)

        # AFTER indexing: a crash between the two leaves content outdated rather
        # than missing. See the module docstring.
        deleted = 0
        if diff.to_delete:
            # RunbookDiff holds frozensets because it is an immutable value; the
            # index takes a mutable set. Converting here keeps the value object
            # immutable rather than loosening it for a storage-layer signature.
            deleted = await self._index.delete_chunks(set(diff.to_delete), refresh=True)

        # LAST: the manifest only claims what Elasticsearch already holds.
        await self._record_indexed(chunks, document_hash)

        return IndexRunResult(
            embedded=result.embedded + embedded,
            indexed=result.indexed + indexed,
            deleted=result.deleted + deleted,
            processed=result.processed + 1,
            skipped=result.skipped,
            removed=result.removed,
        )

    async def _remove_runbook(
        self, runbook_id: str, result: IndexRunResult
    ) -> IndexRunResult:
        stored = await self._index.chunk_ids_for(runbook_id)
        deleted = await self._index.delete_chunks(stored, refresh=True)

        async with self._sessions() as session:
            await session.execute(
                delete(RunbookDocument).where(RunbookDocument.runbook_id == runbook_id)
            )
            await session.commit()

        return IndexRunResult(
            embedded=result.embedded,
            indexed=result.indexed,
            deleted=result.deleted + deleted,
            processed=result.processed,
            skipped=result.skipped,
            removed=result.removed + 1,
        )

    async def _record_indexed(self, chunks: list[Chunk], document_hash: str) -> None:
        """Upsert the manifest row from the chunks that were just indexed.

        Metadata comes off the chunks rather than being re-parsed: they carry the
        frontmatter already, and taking it from the same objects that were
        indexed means the manifest cannot describe something different from what
        is in the index. ``services`` in particular is the GIN-indexed
        pre-filter key.
        """
        first = chunks[0]
        async with self._sessions() as session:
            existing = await session.scalar(
                select(RunbookDocument).where(
                    RunbookDocument.runbook_id == first.runbook_id
                )
            )
            now = utcnow()
            if existing is None:
                session.add(
                    RunbookDocument(
                        runbook_id=first.runbook_id,
                        title=first.title,
                        file_path=str(self._corpus_dir / f"{first.runbook_id}.md"),
                        services=list(first.services),
                        severity=first.severity,
                        content_hash=document_hash,
                        chunk_count=len(chunks),
                        indexed_at=now,
                        index_status="indexed",
                        updated_at=now,
                    )
                )
            else:
                existing.title = first.title
                existing.services = list(first.services)
                existing.severity = first.severity
                existing.content_hash = document_hash
                existing.chunk_count = len(chunks)
                existing.indexed_at = now
                existing.index_status = "indexed"
                existing.updated_at = now
            await session.commit()
