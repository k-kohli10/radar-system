"""Runbook markdown -> stable, content-addressed chunks.

Pure functions, zero I/O: everything here takes a string and returns values.
Reading files, talking to Elasticsearch, and calling the gateway all belong to
callers, which is what makes this module exhaustively testable without fixtures
or fakes.

The chunking contract is ``docs/runbooks/README.md``:

- One chunk per ``##`` (H2) section. The H2 sections ARE the chunk boundaries.
- The document title is prepended to each chunk as a breadcrumb, so a chunk
  retrieved on its own still says what it belongs to.
- **No overlap between chunks**, deliberately. Overlap rescues fixed-window
  chunking from cutting mid-thought; semantic section boundaries do not have
  that problem, and overlap would corrode incremental indexing — an edit to one
  section would change its neighbours' content, and therefore their ids, and
  therefore re-embed them too.

``###`` splitting for oversized sections is specified in the runbook README as
the designated boundary but is deliberately NOT implemented: the corpus contains
no ``###`` headings and its largest chunk uses about 5% of the embedding model's
input budget, so a splitting path here could not be exercised by any real
content. The indexer instead asserts each chunk fits the budget and fails loudly
if one ever does not.

Chunk identity is a content hash, and its stability is what makes incremental
indexing work: re-chunking unchanged content must produce byte-identical ids, or
every indexing run would re-embed the whole corpus. See :func:`compute_chunk_id`.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any

import yaml

#: Frontmatter fields every runbook carries. ``alert_name`` is Tier-1 only.
_REQUIRED_FIELDS = ("runbook_id", "title", "services", "severity", "status")

_FRONTMATTER = re.compile(r"\A---\n(.*?)\n---\n(.*)\Z", re.S)
_H2 = re.compile(r"^## (.+)$", re.M)

#: Separates the fields going into a chunk id. A NUL byte cannot occur in the
#: markdown source, so no combination of field values can collide by running
#: two fields together (``"ab"+"c"`` vs ``"a"+"bc"``).
_ID_SEPARATOR = b"\x00"


class ChunkingError(ValueError):
    """A runbook could not be chunked.

    Raised rather than returning a partial result: a runbook that cannot be
    parsed must not be indexed as an empty or truncated document, which would
    silently remove it from retrieval while looking like a success.
    """


@dataclass(frozen=True)
class RunbookFrontmatter:
    """The parsed frontmatter block of a runbook."""

    runbook_id: str
    title: str
    services: tuple[str, ...]
    severity: str
    status: str
    alert_name: str | None


@dataclass(frozen=True)
class Chunk:
    """One embeddable unit: a single H2 section with its title breadcrumb.

    ``text`` is exactly what gets embedded and exactly what ``chunk_id`` is
    computed over, so the id cannot drift from the content it names.
    """

    chunk_id: str
    runbook_id: str
    title: str
    section: str
    text: str
    ordinal: int
    services: tuple[str, ...]
    severity: str
    alert_name: str | None


def unify_line_endings(text: str) -> str:
    """Convert CRLF and CR to LF.

    Applied to the whole source before anything parses it. Every pattern in this
    module anchors on ``\\n``, so a CRLF checkout would otherwise fail to match
    the frontmatter delimiters at all and make every runbook unparseable — and
    would leave a stray ``\\r`` on the end of each H2 section name where it did
    match.
    """
    return text.replace("\r\n", "\n").replace("\r", "\n")


def normalize(text: str) -> str:
    """Canonicalise text so incidental edits do not change a chunk's identity.

    Line endings are unified and trailing whitespace removed, because a CRLF
    checkout or an editor stripping (or adding) trailing spaces would otherwise
    change every hash and re-embed the entire corpus for no semantic reason.

    Deliberately conservative: interior blank lines, interior indentation, and
    wording are all preserved, so a genuine content edit still changes the hash.
    The normalisation removes only differences that cannot alter meaning.
    """
    unified = unify_line_endings(text)
    return "\n".join(line.rstrip() for line in unified.split("\n")).strip()


def compute_chunk_id(runbook_id: str, section: str, text: str) -> str:
    """Content-addressed id for one chunk: ``sha256(runbook_id, section, text)``.

    **This function's stability is the foundation of incremental indexing.**
    Identical inputs must produce an identical id on every run and every
    machine, because the indexer decides what to re-embed by comparing ids. An
    id that varied with anything else — insertion order, a timestamp, a
    dict iteration — would re-embed the whole corpus on every run while looking
    like it was working.

    ``runbook_id`` is included even though ``text`` already carries the title
    breadcrumb: two runbooks could legitimately hold an identical section, and
    they must remain distinct documents in the index.
    """
    digest = hashlib.sha256()
    for field in (runbook_id, section, text):
        digest.update(field.encode("utf-8"))
        digest.update(_ID_SEPARATOR)
    return digest.hexdigest()


def compute_document_hash(source: str) -> str:
    """Content hash for a whole runbook file, for the Postgres manifest.

    Normalised like chunk text, so the file-level and chunk-level hashes agree
    about what counts as a change. Lets the indexer skip an unchanged file
    without chunking it at all.
    """
    return hashlib.sha256(normalize(source).encode("utf-8")).hexdigest()


def parse_frontmatter(source: str) -> tuple[RunbookFrontmatter, str]:
    """Split a runbook into its frontmatter and its body.

    The frontmatter is PARSED, never chunked: only the body is split into
    chunks, and only the parsed ``title`` value reaches the chunk text. That is
    what makes frontmatter field order irrelevant to chunk identity.
    """
    match = _FRONTMATTER.match(unify_line_endings(source))
    if match is None:
        raise ChunkingError(
            "runbook has no YAML frontmatter block; every runbook must open "
            "with one (see docs/runbooks/README.md)"
        )

    try:
        parsed: Any = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        # Never chain: parser errors quote source lines, and the message is
        # what ends up in logs.
        raise ChunkingError(
            f"runbook frontmatter is not valid YAML: {exc.args[0]}"
        ) from None

    if not isinstance(parsed, dict):
        raise ChunkingError("runbook frontmatter is not a YAML mapping")

    missing = [field for field in _REQUIRED_FIELDS if field not in parsed]
    if missing:
        raise ChunkingError(f"runbook frontmatter is missing {missing}")

    services = parsed["services"]
    if not isinstance(services, list) or not services:
        raise ChunkingError("runbook `services` must be a non-empty list")

    return (
        RunbookFrontmatter(
            runbook_id=str(parsed["runbook_id"]),
            title=str(parsed["title"]),
            services=tuple(str(service) for service in services),
            severity=str(parsed["severity"]),
            status=str(parsed["status"]),
            alert_name=(
                str(parsed["alert_name"])
                if parsed.get("alert_name") is not None
                else None
            ),
        ),
        match.group(2),
    )


def split_sections(body: str) -> list[tuple[str, str]]:
    """Split a runbook body into ``(section_name, section_body)`` on H2s.

    Content before the first ``##`` — the H1 title line — is dropped: it
    duplicates the frontmatter title, which is already prepended to every chunk
    as a breadcrumb.
    """
    names = _H2.findall(body)
    if not names:
        raise ChunkingError("runbook has no `##` sections, so it has no chunks")

    # [1:] drops everything before the first H2 (the H1 title line).
    bodies = re.split(r"^## .+$", body, flags=re.M)[1:]
    return list(zip(names, bodies, strict=True))


def build_chunk_text(title: str, section: str, body: str) -> str:
    """Assemble the exact string that gets embedded.

    The title breadcrumb is what lets a chunk stand on its own: retrieved in
    isolation, ``"Order Service High Memory — Investigation"`` still says which
    runbook and which part of it this is.
    """
    return normalize(f"{title} — {section}\n\n{normalize(body)}")


def chunk_runbook(source: str) -> list[Chunk]:
    """Chunk one runbook's markdown into embeddable, content-addressed chunks.

    The single entry point: parse frontmatter, split on H2, build each chunk's
    text, and hash it. Pure — same input, same output, always.
    """
    frontmatter, body = parse_frontmatter(source)

    chunks: list[Chunk] = []
    for ordinal, (section, section_body) in enumerate(split_sections(body)):
        text = build_chunk_text(frontmatter.title, section, section_body)
        chunks.append(
            Chunk(
                chunk_id=compute_chunk_id(frontmatter.runbook_id, section, text),
                runbook_id=frontmatter.runbook_id,
                title=frontmatter.title,
                section=section,
                text=text,
                ordinal=ordinal,
                services=frontmatter.services,
                severity=frontmatter.severity,
                alert_name=frontmatter.alert_name,
            )
        )
    return chunks
