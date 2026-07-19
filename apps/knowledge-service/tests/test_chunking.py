"""Chunking is pure, so it is tested directly rather than through fakes.

The load-bearing property here is **chunk id stability**. Incremental indexing
decides what to re-embed by comparing content hashes, so an id that changes when
the content did not would re-embed the entire corpus on every run — while
looking like it was working. Most of this module exists to pin that down.
"""

from __future__ import annotations

import pytest
from radar_knowledge_service.chunking import (
    Chunk,
    ChunkingError,
    build_chunk_text,
    chunk_runbook,
    compute_chunk_id,
    compute_document_hash,
    normalize,
    parse_frontmatter,
    split_sections,
)

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
- Heap growing through the overnight trough.

## Related

- `order-service-high-failure-rate` — different failure mode.
"""

TIER2 = """\
---
runbook_id: depth-runbook
title: Depth Runbook
services:
  - order-service
severity: high
status: fixture
---

# Depth Runbook

## Summary

No alert fires this one.
"""


# --------------------------------------------------------------- frontmatter


def test_parse_frontmatter_reads_every_field() -> None:
    frontmatter, body = parse_frontmatter(RUNBOOK)

    assert frontmatter.runbook_id == "order-service-high-memory"
    assert frontmatter.title == "Order Service High Memory"
    assert frontmatter.alert_name == "OrderServiceHighMemory"
    assert frontmatter.services == ("order-service",)
    assert frontmatter.severity == "medium"
    assert frontmatter.status == "fixture"
    assert body.lstrip().startswith("# Order Service High Memory")


def test_alert_name_is_optional_for_depth_runbooks() -> None:
    """Tier-2 runbooks carry no alert; that is a valid runbook, not an error."""
    frontmatter, _ = parse_frontmatter(TIER2)

    assert frontmatter.alert_name is None


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("no frontmatter here", "no YAML frontmatter"),
        ("---\n: : :\n---\nbody\n", "not valid YAML"),
        ("---\n- a\n- b\n---\nbody\n", "not a YAML mapping"),
        ("---\ntitle: T\n---\nbody\n", "missing"),
    ],
)
def test_malformed_frontmatter_raises_rather_than_returning_partial(
    source: str, expected: str
) -> None:
    """A runbook that cannot be parsed must not index as an empty document.

    Returning a partial result would silently drop the runbook from retrieval
    while the indexing run reported success.
    """
    with pytest.raises(ChunkingError, match=expected):
        parse_frontmatter(source)


def test_empty_services_list_is_rejected() -> None:
    """`services` is the retrieval pre-filter key; empty means unretrievable."""
    source = RUNBOOK.replace("services:\n  - order-service", "services: []")

    with pytest.raises(ChunkingError, match="non-empty list"):
        parse_frontmatter(source)


# ------------------------------------------------------------------ sections


def test_split_sections_splits_on_h2_and_drops_the_h1() -> None:
    _, body = parse_frontmatter(RUNBOOK)

    sections = split_sections(body)

    assert [name for name, _ in sections] == ["Summary", "Symptoms", "Related"]
    assert "# Order Service High Memory" not in "".join(b for _, b in sections)


def test_a_runbook_with_no_h2_sections_is_an_error() -> None:
    with pytest.raises(ChunkingError, match="no `##` sections"):
        split_sections("# Title\n\nProse with no sections.\n")


def test_h3_headings_stay_inside_their_parent_section() -> None:
    """H3 splitting is deliberately unimplemented; H3s ride along with the H2."""
    body = "\n## Investigation\n\n### First\n\nstep one\n\n### Second\n\nstep two\n"

    sections = split_sections(body)

    assert len(sections) == 1
    assert "### First" in sections[0][1]
    assert "### Second" in sections[0][1]


# ------------------------------------------------------------------ chunking


def test_chunk_runbook_produces_one_chunk_per_section() -> None:
    chunks = chunk_runbook(RUNBOOK)

    assert [c.section for c in chunks] == ["Summary", "Symptoms", "Related"]
    assert [c.ordinal for c in chunks] == [0, 1, 2]
    assert all(c.runbook_id == "order-service-high-memory" for c in chunks)


def test_every_chunk_carries_the_title_breadcrumb() -> None:
    """A chunk retrieved alone must still say what it belongs to."""
    for chunk in chunk_runbook(RUNBOOK):
        assert chunk.text.startswith(f"Order Service High Memory — {chunk.section}")


def test_chunks_do_not_overlap() -> None:
    """No-overlap is what keeps incremental indexing incremental.

    With overlap, editing one section would change its neighbours' text, hence
    their ids, hence re-embed them too.
    """
    chunks = chunk_runbook(RUNBOOK)

    assert "Restarts climbing" in chunks[1].text
    assert "Restarts climbing" not in chunks[0].text
    assert "Restarts climbing" not in chunks[2].text
    assert "five minutes" not in chunks[1].text


def test_carried_frontmatter_is_identical_on_every_chunk() -> None:
    for chunk in chunk_runbook(RUNBOOK):
        assert chunk.services == ("order-service",)
        assert chunk.severity == "medium"
        assert chunk.alert_name == "OrderServiceHighMemory"


def test_chunks_are_frozen() -> None:
    """Chunks are values. A mutated chunk would no longer match its own id."""
    chunk = chunk_runbook(RUNBOOK)[0]

    with pytest.raises(Exception):
        chunk.text = "mutated"  # type: ignore[misc]


# --------------------------------------------------------------- id stability


def test_chunk_ids_are_stable_across_runs() -> None:
    """THE load-bearing property: same content in, same ids out.

    If this fails, every indexing run re-embeds the whole corpus.
    """
    first = [c.chunk_id for c in chunk_runbook(RUNBOOK)]
    second = [c.chunk_id for c in chunk_runbook(RUNBOOK)]

    assert first == second


def test_chunk_ids_are_unaffected_by_frontmatter_field_order() -> None:
    """Frontmatter is parsed, never chunked, so field order cannot reach a hash.

    This is what makes field order a tidiness question rather than a correctness
    one — and why no test guards the order itself.
    """
    reordered = RUNBOOK.replace(
        "severity: medium\nstatus: fixture",
        "status: fixture\nseverity: medium",
    )

    assert [c.chunk_id for c in chunk_runbook(reordered)] == [
        c.chunk_id for c in chunk_runbook(RUNBOOK)
    ]


def test_editing_one_section_changes_only_that_chunks_id() -> None:
    """The incremental-indexing property, stated as a test.

    One section edited must mean exactly one chunk re-embedded.
    """
    before = chunk_runbook(RUNBOOK)
    after = chunk_runbook(RUNBOOK.replace("Restarts climbing", "Restarts flat"))

    changed = [
        (b.section, b.chunk_id != a.chunk_id)
        for b, a in zip(before, after, strict=True)
    ]

    assert changed == [("Summary", False), ("Symptoms", True), ("Related", False)]


def test_line_ending_and_trailing_whitespace_changes_do_not_change_ids() -> None:
    """A CRLF checkout must not re-embed the corpus."""
    crlf = RUNBOOK.replace("\n", "\r\n")
    trailing = RUNBOOK.replace("Restarts climbing.", "Restarts climbing.   ")

    baseline = [c.chunk_id for c in chunk_runbook(RUNBOOK)]

    assert [c.chunk_id for c in chunk_runbook(crlf)] == baseline
    assert [c.chunk_id for c in chunk_runbook(trailing)] == baseline


def test_normalization_preserves_meaningful_differences() -> None:
    """Conservative on purpose: it must not normalise away real edits."""
    assert normalize("a\r\nb  ") == "a\nb"
    assert normalize("a\n\nb") != normalize("a\nb")  # blank lines are content
    # Interior indentation is content (markdown list nesting depends on it).
    # Leading whitespace at the very start of the text is not — it is stripped
    # along with trailing, which is why this is stated on line two, not line one.
    assert normalize("a\n  indented") != normalize("a\nindented")


def test_same_section_in_different_runbooks_gets_different_ids() -> None:
    """Two runbooks may legitimately share a section; they stay distinct docs."""
    text = build_chunk_text("Title", "Summary", "identical body")

    assert compute_chunk_id("runbook-a", "Summary", text) != compute_chunk_id(
        "runbook-b", "Summary", text
    )


def test_chunk_id_fields_cannot_run_together() -> None:
    """Field boundaries are explicit, so no two field splits can collide."""
    assert compute_chunk_id("ab", "c", "x") != compute_chunk_id("a", "bc", "x")


def test_chunk_id_matches_a_hash_of_its_own_text() -> None:
    """The id names the content it is stored with — it cannot drift from it."""
    for chunk in chunk_runbook(RUNBOOK):
        assert chunk.chunk_id == compute_chunk_id(
            chunk.runbook_id, chunk.section, chunk.text
        )


# ----------------------------------------------------------- document hashing


def test_document_hash_is_stable_and_content_sensitive() -> None:
    assert compute_document_hash(RUNBOOK) == compute_document_hash(RUNBOOK)
    assert compute_document_hash(RUNBOOK) != compute_document_hash(
        RUNBOOK.replace("five minutes", "ten minutes")
    )


def test_document_hash_ignores_the_same_noise_chunk_ids_ignore() -> None:
    """File-level and chunk-level hashes must agree on what a change is.

    If the document hash saw a CRLF change that chunk ids did not, the indexer
    would re-chunk the file, find nothing changed, and do redundant work.
    """
    assert compute_document_hash(
        RUNBOOK.replace("\n", "\r\n")
    ) == compute_document_hash(RUNBOOK)


# ------------------------------------------------------------ the real corpus


def test_the_real_corpus_chunks_without_error() -> None:
    """Exercise every committed runbook, not just the fixture above."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[3] / "docs" / "runbooks"
    paths = sorted(p for p in root.glob("*.md") if p.name != "README.md")

    assert paths, f"no runbooks found under {root} — this test just stopped checking"

    for path in paths:
        chunks = chunk_runbook(path.read_text())
        assert len(chunks) == 8, f"{path.name} produced {len(chunks)} chunks"
        assert all(isinstance(c, Chunk) for c in chunks)
        assert len({c.chunk_id for c in chunks}) == 8, f"{path.name} has duplicate ids"
