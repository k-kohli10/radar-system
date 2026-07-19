"""Reconciliation: what an indexing run decides to do, and how much work it costs.

**These tests assert on WORK, not on final state.** That distinction is the whole
point of the module. Re-embedding the entire corpus on every run produces a
completely correct index — so "is the index right afterwards?" passes for both
the incremental implementation and the full-rebuild one, and cannot tell them
apart. The bug is not a wrong index; the bug is the amount of work done to get a
right one.

So every test here counts: how many chunks were selected for embedding, and
WHICH ones. Those numbers are what change when the scale property is broken, and
they stay wrong quietly — at 17 runbooks a full rebuild is 136 calls and a few
seconds, which looks fine. At 200 it is thousands of calls per run.
"""

from __future__ import annotations

from radar_knowledge_service.chunking import chunk_runbook
from radar_knowledge_service.reconciliation import diff_corpus, diff_runbook

RUNBOOK = """\
---
runbook_id: order-service-high-memory
title: Order Service High Memory
alert_name: OrderServiceHighMemory
services:
  - order-service
severity: medium
status: fixture
---

# Order Service High Memory

## Summary

Memory has stayed above the threshold for five minutes.

## Symptoms

- Restarts climbing.

## Investigation

- Read the 24-hour shape.

## Related

- `order-service-high-failure-rate` — different failure mode.
"""


def _ids(source: str = RUNBOOK) -> list[str]:
    return [c.chunk_id for c in chunk_runbook(source)]


# ------------------------------------------------------- corpus-level diff


def test_an_unchanged_corpus_is_a_complete_no_op() -> None:
    """Second run with no edits: nothing is chunked, nothing is embedded.

    Mutation this catches: ignoring the manifest hash and processing every file
    anyway. The index would end up identical — and every run would re-embed the
    whole corpus.
    """
    hashes = {"a": "h1", "b": "h2", "c": "h3"}

    diff = diff_corpus(on_disk=hashes, manifest=hashes)

    assert diff.is_noop
    assert diff.changed == ()
    assert diff.unchanged == ("a", "b", "c")
    assert diff.removed == ()


def test_editing_one_file_marks_only_that_file_changed() -> None:
    """One edit must not drag the rest of the corpus into the run."""
    diff = diff_corpus(
        on_disk={"a": "h1", "b": "CHANGED", "c": "h3"},
        manifest={"a": "h1", "b": "h2", "c": "h3"},
    )

    assert diff.changed == ("b",)
    assert diff.unchanged == ("a", "c")
    assert diff.removed == ()


def test_a_new_file_is_the_only_thing_processed() -> None:
    """Adding a runbook must not rebuild the ones already indexed."""
    diff = diff_corpus(
        on_disk={"a": "h1", "b": "h2", "new": "h9"},
        manifest={"a": "h1", "b": "h2"},
    )

    assert diff.changed == ("new",)
    assert diff.unchanged == ("a", "b")
    assert diff.removed == ()


def test_a_deleted_file_is_reported_as_removed() -> None:
    """A runbook deleted from disk must not stay retrievable forever.

    Without this the reasoner would ground an RCA in documentation that no
    longer exists — and nothing would ever report an error.
    """
    diff = diff_corpus(
        on_disk={"a": "h1"},
        manifest={"a": "h1", "gone": "h2"},
    )

    assert diff.removed == ("gone",)
    assert diff.changed == ()
    assert diff.unchanged == ("a",)


def test_an_empty_manifest_makes_every_file_changed() -> None:
    """First run: nothing is indexed yet, so everything is work to do."""
    diff = diff_corpus(on_disk={"a": "h1", "b": "h2"}, manifest={})

    assert diff.changed == ("a", "b")
    assert diff.unchanged == ()
    assert not diff.is_noop


def test_add_edit_and_delete_in_one_run_are_kept_apart() -> None:
    diff = diff_corpus(
        on_disk={"kept": "h1", "edited": "NEW", "added": "h9"},
        manifest={"kept": "h1", "edited": "h2", "deleted": "h3"},
    )

    assert diff.changed == ("added", "edited")
    assert diff.unchanged == ("kept",)
    assert diff.removed == ("deleted",)


# ------------------------------------------------------ runbook-level diff


def test_a_first_time_runbook_embeds_every_chunk() -> None:
    chunks = chunk_runbook(RUNBOOK)

    diff = diff_runbook("order-service-high-memory", chunks, stored_ids=[])

    assert diff.embed_count == len(chunks) == 4
    assert diff.to_delete == frozenset()


def test_an_unchanged_runbook_embeds_nothing() -> None:
    """ZERO embedding calls, not 'few'.

    Mutation this catches: embedding every produced chunk regardless of what is
    stored. The index would be correct and the run would cost a full rebuild.
    """
    chunks = chunk_runbook(RUNBOOK)

    diff = diff_runbook("order-service-high-memory", chunks, stored_ids=_ids())

    assert diff.embed_count == 0
    assert diff.is_noop
    assert diff.unchanged == frozenset(_ids())
    assert diff.to_delete == frozenset()


def test_editing_one_section_embeds_exactly_one_chunk() -> None:
    """THE central property, stated as a count.

    Four sections, one edited: one embedding call. Not four. The mutation that
    breaks this — re-embed all produced chunks — leaves the index correct, so
    only the count catches it.
    """
    edited = RUNBOOK.replace("Restarts climbing.", "Restarts flat.")
    stored = _ids(RUNBOOK)

    diff = diff_runbook("order-service-high-memory", chunk_runbook(edited), stored)

    assert diff.embed_count == 1
    assert diff.to_embed[0].section == "Symptoms"
    assert len(diff.unchanged) == 3
    # The old Symptoms chunk is superseded and must not linger.
    assert len(diff.to_delete) == 1


def test_the_edited_chunk_is_the_only_one_not_reused() -> None:
    """Name the untouched sections, so a broad re-embed cannot pass."""
    edited = RUNBOOK.replace("Restarts climbing.", "Restarts flat.")
    before = {c.section: c.chunk_id for c in chunk_runbook(RUNBOOK)}

    diff = diff_runbook("order-service-high-memory", chunk_runbook(edited), _ids())

    reused = diff.unchanged
    assert before["Summary"] in reused
    assert before["Investigation"] in reused
    assert before["Related"] in reused
    assert before["Symptoms"] not in reused


def test_removing_a_section_deletes_only_that_chunk() -> None:
    trimmed = RUNBOOK.replace("## Investigation\n\n- Read the 24-hour shape.\n\n", "")
    before = {c.section: c.chunk_id for c in chunk_runbook(RUNBOOK)}

    diff = diff_runbook("order-service-high-memory", chunk_runbook(trimmed), _ids())

    assert diff.embed_count == 0
    assert diff.to_delete == frozenset({before["Investigation"]})
    assert len(diff.unchanged) == 3


def test_adding_a_section_embeds_only_the_new_one() -> None:
    extended = RUNBOOK + "\n## Escalation\n\nPage the on-call.\n"

    diff = diff_runbook("order-service-high-memory", chunk_runbook(extended), _ids())

    assert diff.embed_count == 1
    assert diff.to_embed[0].section == "Escalation"
    assert diff.to_delete == frozenset()


def test_stale_chunks_from_a_deleted_runbook_are_all_removed() -> None:
    """Nothing produced, everything stored: delete it all, embed nothing."""
    diff = diff_runbook("order-service-high-memory", [], stored_ids=_ids())

    assert diff.embed_count == 0
    assert diff.to_delete == frozenset(_ids())
    assert diff.unchanged == frozenset()


def test_reordering_sections_reuses_every_chunk() -> None:
    """Chunk identity is content, not position.

    Moving a section must not re-embed it — ``ordinal`` is metadata, not part of
    the id, and this is what pins that decision.
    """
    chunks = chunk_runbook(RUNBOOK)
    reversed_chunks = list(reversed(chunks))

    diff = diff_runbook("order-service-high-memory", reversed_chunks, _ids())

    assert diff.embed_count == 0
    assert diff.is_noop


def test_the_real_corpus_re_runs_as_a_complete_no_op() -> None:
    """The scale property against every committed runbook, not a fixture.

    Index the corpus once (conceptually), then re-run: zero embedding calls
    across all 17 runbooks. A full-rebuild implementation would report 136.
    """
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "docs" / "runbooks"
    paths = sorted(p for p in root.glob("*.md") if p.name != "README.md")
    assert paths, f"no runbooks under {root} — this test just stopped checking"

    total_chunks = 0
    total_embeds = 0
    for path in paths:
        chunks = chunk_runbook(path.read_text())
        total_chunks += len(chunks)
        stored = [c.chunk_id for c in chunks]  # already indexed
        diff = diff_runbook(path.stem, chunks, stored)
        total_embeds += diff.embed_count

    assert total_chunks == 136, total_chunks
    assert total_embeds == 0, f"a re-run cost {total_embeds} embedding calls"
