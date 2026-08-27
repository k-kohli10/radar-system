"""Deciding what work an indexing run actually needs to do.

Pure functions, zero I/O, the same shape as ``chunking``. Given what is on disk
and what is already stored, these decide what to embed, what to delete, and what
to leave alone. The caller performs the work; nothing here talks to Postgres,
Elasticsearch, or the gateway.

**This module is where the scale property lives.** Re-embedding the whole corpus
on every run would produce a perfectly correct index, which is what makes it
dangerous: a correctness test cannot tell the two apart. At 17 runbooks a full
rebuild costs 136 embedding calls and a few seconds; at 200 runbooks it is
thousands of calls per run and a bill. The difference is visible only in how much
work was done, so that is what the tests assert on.

Two levels of diff, because there are two levels of change:

- :func:`diff_corpus` compares file content hashes against the Postgres manifest,
  so an unchanged runbook is skipped **without being chunked at all**, and a
  runbook deleted from disk is noticed rather than silently orphaned in the index.
- :func:`diff_runbook` compares the chunk ids a runbook currently produces against
  the ids stored for it, so editing one section re-embeds one chunk. This is the
  step the chunker's id stability exists to make possible.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from radar_knowledge_service.chunking import Chunk


@dataclass(frozen=True)
class CorpusDiff:
    """Which runbooks need looking at, and which have gone away.

    ``unchanged`` is the interesting one: those files are never opened, chunked,
    or embedded. On a re-run with no edits, every runbook lands here and the run
    does no work at all.
    """

    changed: tuple[str, ...]
    unchanged: tuple[str, ...]
    removed: tuple[str, ...]

    @property
    def is_noop(self) -> bool:
        """True when nothing on disk differs from the manifest."""
        return not self.changed and not self.removed


@dataclass(frozen=True)
class RunbookDiff:
    """What one runbook needs: chunks to embed, chunk ids to drop.

    ``to_embed`` holds whole :class:`Chunk` objects because they still need
    vectors. ``to_delete`` holds bare ids because deletion needs nothing else.
    """

    runbook_id: str
    to_embed: tuple[Chunk, ...]
    unchanged: frozenset[str]
    to_delete: frozenset[str]

    @property
    def embed_count(self) -> int:
        """How many embedding calls this runbook will cost."""
        return len(self.to_embed)

    @property
    def is_noop(self) -> bool:
        """True when the stored chunks already match what the file produces."""
        return not self.to_embed and not self.to_delete


def diff_corpus(
    *,
    on_disk: Mapping[str, str],
    manifest: Mapping[str, str],
) -> CorpusDiff:
    """Compare runbook content hashes on disk against the stored manifest.

    ``on_disk`` and ``manifest`` both map ``runbook_id`` to a document hash (see
    ``chunking.compute_document_hash``). A runbook whose hash is unchanged is
    skipped entirely, meaning not chunked, not diffed, not embedded, which is
    what makes a no-change run cost nothing.

    A runbook present in the manifest but absent from disk was deleted, and its
    chunks must be removed. Without this, deleting a runbook would leave its
    chunks retrievable forever, and the reasoner would ground an RCA in
    documentation that no longer exists.
    """
    changed = sorted(
        runbook_id
        for runbook_id, digest in on_disk.items()
        if manifest.get(runbook_id) != digest
    )
    unchanged = sorted(
        runbook_id
        for runbook_id, digest in on_disk.items()
        if manifest.get(runbook_id) == digest
    )
    removed = sorted(set(manifest) - set(on_disk))
    return CorpusDiff(
        changed=tuple(changed),
        unchanged=tuple(unchanged),
        removed=tuple(removed),
    )


def diff_runbook(
    runbook_id: str,
    chunks: Iterable[Chunk],
    stored_ids: Iterable[str],
) -> RunbookDiff:
    """Decide which of a runbook's chunks need embedding, and which are stale.

    Chunk ids are content hashes, so set membership IS change detection:

    - an id the file produces that is not stored -> its content is new or edited,
      so it needs a vector;
    - an id that is both produced and stored -> byte-identical content already in
      the index, so **it must not be embedded again**;
    - an id stored but no longer produced -> the section was edited away or
      renamed, so it is deleted.

    The middle case is the whole point. Editing one section of a runbook leaves
    its other sections' ids untouched, so a one-section edit costs exactly one
    embedding call rather than eight.
    """
    produced = {chunk.chunk_id: chunk for chunk in chunks}
    stored = set(stored_ids)

    return RunbookDiff(
        runbook_id=runbook_id,
        to_embed=tuple(
            chunk for chunk_id, chunk in produced.items() if chunk_id not in stored
        ),
        unchanged=frozenset(produced.keys() & stored),
        to_delete=frozenset(stored - produced.keys()),
    )
