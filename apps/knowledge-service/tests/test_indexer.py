"""The indexing run: is the proven core wired up correctly?

The decisions were proven in ``test_reconciliation.py`` without any I/O. These
tests do not re-prove them. They prove the ORCHESTRATION: that the run calls the
right things, in the right order, with the right effects, and that failures
propagate instead of being swallowed.

Two things get real infrastructure rather than fakes:

- **Postgres**, because the manifest is what makes a re-run cheap, and "the hash
  survives a round trip" is a database question. A fake dict would pass whether
  or not the column, the upsert, or the transaction worked.
- The **index and embedder are fakes**, deliberately: they RECORD CALLS. The
  guarantee under test is how much work the run did and in what order, which is
  a property of the calls, not of Elasticsearch.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

import pytest
from radar_database import Database, RunbookDocument
from radar_knowledge_service.indexer import (
    DimensionMismatchError,
    RunbookIndexer,
    chunk_to_document,
    read_corpus,
)
from sqlalchemy import select

pytestmark = pytest.mark.asyncio

CORPUS = Path(__file__).resolve().parents[3] / "docs" / "runbooks"
DIMS = 4


class FakeIndex:
    """Records every call, and keeps just enough state to answer honestly."""

    def __init__(self, *, dims: int = DIMS) -> None:
        self.documents: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple[str, Any]] = []
        self._dims = dims

    async def ensure_index(self) -> bool:
        self.calls.append(("ensure_index", None))
        return True

    async def verify_dims(self) -> int:
        self.calls.append(("verify_dims", None))
        return self._dims

    async def index(
        self, documents: list[dict[str, Any]], *, refresh: bool = False
    ) -> int:
        self.calls.append(("index", [d["chunk_id"] for d in documents]))
        for document in documents:
            self.documents[document["chunk_id"]] = document
        return len(documents)

    async def chunk_ids_for(self, runbook_id: str) -> set[str]:
        self.calls.append(("chunk_ids_for", runbook_id))
        return {
            chunk_id
            for chunk_id, document in self.documents.items()
            if document["runbook_id"] == runbook_id
        }

    async def delete_chunks(self, chunk_ids: set[str], *, refresh: bool = False) -> int:
        self.calls.append(("delete_chunks", set(chunk_ids)))
        for chunk_id in chunk_ids:
            self.documents.pop(chunk_id, None)
        return len(chunk_ids)


class FakeEmbedder:
    """Counts embedding calls — the number the scale guarantee is about."""

    def __init__(self, *, dims: int = DIMS) -> None:
        self.dims = dims
        self.batches: list[list[str]] = []
        self.budget_checks: list[list[str]] = []

    def check_budget(self, texts: list[str]) -> None:
        self.budget_checks.append(list(texts))

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.batches.append(list(texts))
        return [[0.1] * self.dims for _ in texts]

    @property
    def embedded_count(self) -> int:
        return sum(len(batch) for batch in self.batches)


def _corpus_copy(tmp_path: Path, *names: str) -> Path:
    """A throwaway corpus directory holding real runbooks."""
    directory = tmp_path / "runbooks"
    directory.mkdir()
    for name in names:
        shutil.copy(CORPUS / f"{name}.md", directory / f"{name}.md")
    return directory


def _edit(path: Path, old: str, new: str) -> None:
    """Edit a runbook, refusing to be a silent no-op.

    ``str.replace`` with an absent anchor changes nothing and raises nothing, so
    a test that "edits a file" and then asserts one chunk was re-embedded would
    instead be asserting against an unmodified file — and would fail for a
    reason that looks like the indexer being wrong. The assertion makes the
    anchor's absence the error it actually is.
    """
    text = path.read_text()
    assert old in text, f"{old!r} is not in {path.name}; this edit would do nothing"
    path.write_text(text.replace(old, new, 1))


def _indexer(db: Database, corpus: Path, index: Any, embedder: Any) -> RunbookIndexer:
    return RunbookIndexer(
        index=index,
        embedder=embedder,
        session_factory=db.session_factory,
        corpus_dir=corpus,
    )


# ------------------------------------------------------------------- reading


def test_read_corpus_skips_the_readme() -> None:
    """README.md documents the corpus; it is not part of it."""
    corpus = read_corpus(CORPUS)

    assert "README" not in corpus
    assert "order-service-high-memory" in corpus
    assert len(corpus) == 17


def test_chunk_to_document_carries_the_prefilter_key() -> None:
    from radar_knowledge_service.chunking import chunk_runbook

    chunk = chunk_runbook((CORPUS / "order-service-high-memory.md").read_text())[0]

    document = chunk_to_document(chunk, [0.1] * DIMS)

    assert document["chunk_id"] == chunk.chunk_id
    assert document["services"] == ["order-service"]
    assert document["alert_name"] == "OrderServiceHighMemory"
    assert document["embedding"] == [0.1] * DIMS


# --------------------------------------------------------------- first run


async def test_a_first_run_embeds_and_indexes_every_chunk(
    db: Database, tmp_path: Path
) -> None:
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")
    index, embedder = FakeIndex(), FakeEmbedder()

    result = await _indexer(db, corpus, index, embedder).run()

    assert result.embedded == 8
    assert result.indexed == 8
    assert result.deleted == 0
    assert result.processed == 1
    assert result.skipped == 0
    assert embedder.embedded_count == 8


async def test_a_first_run_records_the_manifest_with_real_metadata(
    db: Database, tmp_path: Path
) -> None:
    """`services` is the GIN-indexed pre-filter key, not decoration."""
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")

    await _indexer(db, corpus, FakeIndex(), FakeEmbedder()).run()

    async with db.session() as session:
        row = await session.scalar(select(RunbookDocument))

    assert row is not None
    assert row.runbook_id == "order-service-high-memory"
    assert row.title == "Order Service High Memory"
    assert row.services == ["order-service"]
    assert row.severity == "medium"
    assert row.chunk_count == 8
    assert row.index_status == "indexed"
    assert row.indexed_at is not None


# ------------------------------------------------------ the scale guarantee


async def test_a_second_run_with_no_changes_embeds_nothing(
    db: Database, tmp_path: Path
) -> None:
    """THE headline property, through the real manifest.

    Zero embedding calls, and the file is never even chunked — the manifest hash
    short-circuits it.
    """
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")
    index = FakeIndex()

    await _indexer(db, corpus, index, FakeEmbedder()).run()

    second = FakeEmbedder()
    result = await _indexer(db, corpus, index, second).run()

    assert second.embedded_count == 0
    assert result.embedded == 0
    assert result.skipped == 1
    assert result.processed == 0
    assert result.is_noop
    # Not chunked at all: the budget check never saw it.
    assert second.budget_checks == []


async def test_editing_one_section_embeds_exactly_one_chunk(
    db: Database, tmp_path: Path
) -> None:
    """One edit costs one gateway call, not eight — end to end."""
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")
    index = FakeIndex()
    await _indexer(db, corpus, index, FakeEmbedder()).run()

    _edit(
        corpus / "order-service-high-memory.md",
        "page the order-service on-call",
        "page the platform on-call",
    )

    second = FakeEmbedder()
    result = await _indexer(db, corpus, index, second).run()

    assert second.embedded_count == 1
    assert result.embedded == 1
    assert result.deleted == 1  # the superseded chunk
    assert result.processed == 1


async def test_adding_a_runbook_touches_only_the_new_one(
    db: Database, tmp_path: Path
) -> None:
    """A new file must not rebuild the corpus already indexed."""
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")
    index = FakeIndex()
    await _indexer(db, corpus, index, FakeEmbedder()).run()

    shutil.copy(
        CORPUS / "checkout-timeout-rate.md", corpus / "checkout-timeout-rate.md"
    )

    second = FakeEmbedder()
    result = await _indexer(db, corpus, index, second).run()

    assert second.embedded_count == 8  # only the new runbook's chunks
    assert result.skipped == 1  # the existing one untouched
    assert result.processed == 1
    embedded_titles = {text.split(" — ")[0] for b in second.batches for text in b}
    assert embedded_titles == {"Checkout Timeout Rate"}


async def test_deleting_a_runbook_removes_only_its_chunks(
    db: Database, tmp_path: Path
) -> None:
    """A deleted runbook must not stay retrievable forever."""
    corpus = _corpus_copy(
        tmp_path, "order-service-high-memory", "checkout-timeout-rate"
    )
    index = FakeIndex()
    await _indexer(db, corpus, index, FakeEmbedder()).run()
    assert len(index.documents) == 16

    (corpus / "checkout-timeout-rate.md").unlink()

    second = FakeEmbedder()
    result = await _indexer(db, corpus, index, second).run()

    assert result.removed == 1
    assert result.deleted == 8
    assert second.embedded_count == 0
    assert len(index.documents) == 8
    assert all(
        d["runbook_id"] == "order-service-high-memory" for d in index.documents.values()
    )

    async with db.session() as session:
        remaining = (await session.scalars(select(RunbookDocument.runbook_id))).all()
    assert list(remaining) == ["order-service-high-memory"]


# ------------------------------------------------------------------ ordering


async def test_new_chunks_are_indexed_before_stale_ones_are_deleted(
    db: Database, tmp_path: Path
) -> None:
    """A crash between the two must leave content outdated, never missing.

    Deleting first would open a window where a section's old chunk is gone and
    its replacement has not landed.
    """
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")
    index = FakeIndex()
    await _indexer(db, corpus, index, FakeEmbedder()).run()

    _edit(
        corpus / "order-service-high-memory.md",
        "`OrderServiceHighMemory` firing",
        "`OrderServiceHighMemory` alerting",
    )

    index.calls.clear()
    await _indexer(db, corpus, index, FakeEmbedder()).run()

    kinds = [name for name, _ in index.calls]
    assert "index" in kinds and "delete_chunks" in kinds
    assert kinds.index("index") < kinds.index("delete_chunks")


async def test_the_manifest_is_written_after_elasticsearch(
    db: Database, tmp_path: Path
) -> None:
    """If the manifest advanced first and the index write failed, the next run
    would skip the file forever — indexed in name, absent in fact.
    """
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")

    class ExplodingIndex(FakeIndex):
        async def index(
            self, documents: list[dict[str, Any]], *, refresh: bool = False
        ) -> int:
            raise RuntimeError("elasticsearch is down")

    with pytest.raises(RuntimeError, match="elasticsearch is down"):
        await _indexer(db, corpus, ExplodingIndex(), FakeEmbedder()).run()

    async with db.session() as session:
        rows = (await session.scalars(select(RunbookDocument))).all()

    assert list(rows) == [], "the manifest claimed content the index never received"


# ----------------------------------------------------------------- failures


async def test_an_embedding_failure_propagates_and_advances_nothing(
    db: Database, tmp_path: Path
) -> None:
    """Failures raise, per the embed client's decision. Nothing is recorded."""
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")

    class FailingEmbedder(FakeEmbedder):
        async def embed(self, texts: list[str]) -> list[list[float]]:
            raise RuntimeError("the gateway is unavailable")

    with pytest.raises(RuntimeError, match="gateway is unavailable"):
        await _indexer(db, corpus, FakeIndex(), FailingEmbedder()).run()

    async with db.session() as session:
        rows = (await session.scalars(select(RunbookDocument))).all()
    assert list(rows) == []


async def test_a_dimension_mismatch_stops_the_run_before_any_work(
    db: Database, tmp_path: Path
) -> None:
    """Every document would be rejected; the fix is a re-index, not a retry."""
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")
    index = FakeIndex(dims=1536)
    embedder = FakeEmbedder(dims=768)

    with pytest.raises(DimensionMismatchError, match="new index"):
        await _indexer(db, corpus, index, embedder).run()

    assert embedder.embedded_count == 0
    assert index.documents == {}


async def test_the_budget_is_checked_before_embedding(
    db: Database, tmp_path: Path
) -> None:
    """The assertion the chunker defers lands here, ahead of the gateway call."""
    corpus = _corpus_copy(tmp_path, "order-service-high-memory")
    embedder = FakeEmbedder()

    await _indexer(db, corpus, FakeIndex(), embedder).run()

    assert len(embedder.budget_checks) == 1
    assert len(embedder.budget_checks[0]) == 8  # every chunk, not just new ones


async def test_the_whole_real_corpus_indexes_then_re_runs_as_a_noop(
    db: Database, tmp_path: Path
) -> None:
    """All 17 runbooks: 136 chunks once, then zero work."""
    index, first = FakeIndex(), FakeEmbedder()

    result = await _indexer(db, CORPUS, index, first).run()
    assert result.embedded == 136
    assert result.processed == 17

    second = FakeEmbedder()
    again = await _indexer(db, CORPUS, index, second).run()

    assert again.embedded == 0
    assert again.skipped == 17
    assert second.embedded_count == 0
