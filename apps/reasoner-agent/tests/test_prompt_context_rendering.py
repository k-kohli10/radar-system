"""What the model is shown — and what the pipeline's bookkeeping must never reach.

THE BUG THESE TESTS EXIST FOR
------------------------------
The reasoner used to dump the whole context bundle into the user message, so every
retrieved chunk reached the model carrying the knowledge pipeline's own bookkeeping —
``grade`` (CRAG's per-chunk verdict) and ``status`` (``fixture``, because the corpus
has not had a human review pass). The v2 system prompt then told the model to "weight
excerpts graded sufficient over those graded partial".

That instruction is harmless while a bundle contains at least one ``sufficient``
chunk. The grader is not degenerate — 27 ``partial`` to 12 ``sufficient`` across
every chunk RADAR has stored, both grades often in the same bundle — so most
bundles are fine. The failure is the ALL-``partial`` bundle, about one in eight:
there the instruction has nothing to prefer and resolves to *all of your context is
the weaker kind*, and the model sometimes answers by taking the EMPTY-context path
("the root cause of the incident is not covered by any specific runbook") with five
sections of the right runbook in front of it. Retrieval worked, grading worked,
storage worked; the prompt threw the result away.

THE FIXTURE IS THE FAILING CASE, ON PURPOSE
--------------------------------------------
The bundle reconstructed below is the all-``partial`` one — of the 8 recommendations
that ever carried non-empty context, it is the only one with no ``sufficient`` chunk
and the only one that produced the empty-context RCA. 8 of 8, no exceptions. A
fixture built from a mixed-grade bundle would exercise the projection just as well
and would not be the payload that broke.

WHY THE DETERMINISTIC HALF IS THE GUARD — MEASURED, NOT ASSUMED
---------------------------------------------------------------
The symptom is a STOCHASTIC BIAS, not a branch. On that bundle, driving the real
gateway with the pre-fix prompt reproduced the empty-context language in **1 of 20
draws**; the fixed prompt produced it in **0 of 40**.

A behavioural test therefore cannot be the guard: at a ~5% base rate no affordable
number of draws reliably catches a reintroduction. THESE tests are the guard, because
they fail every single time the fix is undone. The behavioural confirmation exists as
well, deliberately weaker and deliberately elsewhere:
``tests/e2e/test_prompt_grade_leak.py``, marked ``live``.

Grades are NOT removed from the system. They still gate inclusion in the knowledge
service and still land in ``recommendations.context_bundle`` for the audit trail —
``tests/e2e/test_knowledge_assisted_rca.py`` asserts exactly that on the stored
wrapper. The only thing that changed is that they stop at the prompt.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from radar_contracts import PlanStep, Severity
from radar_reasoner_agent.context import ContextBundle
from radar_reasoner_agent.llm import (
    PROMPTED_CHUNK_FIELDS,
    SYSTEM_PROMPT,
    render_user_message,
)

#: apps/reasoner-agent/tests/ -> the repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
RUNBOOK = REPO_ROOT / "docs/runbooks/order-service-high-memory.md"

RUNBOOK_ID = "order-service-high-memory"
RUNBOOK_TITLE = "Order Service High Memory"

#: The five sections real retrieval returned for this incident, in the order it
#: returned them. Read off the stored ``context_bundle`` of the recommendation that
#: exhibited the bug — not chosen here, recovered from there.
RETRIEVED_SECTIONS = (
    "Investigation",
    "Symptoms",
    "Resolution",
    "Likely Causes",
    "Summary",
)

#: The grade every chunk in THIS bundle carries — not every chunk in the corpus,
#: where `sufficient` is common. An all-`partial` bundle is what left the pre-fix
#: prompt's grade-weighting clause with nothing to prefer.
CHUNK_GRADE = "partial"
#: ``fixture`` until the corpus gets a human review pass. Leaked alongside the grade.
CHUNK_STATUS = "fixture"

#: Every term that would mean the pipeline's bookkeeping reached the model. Not just
#: the key names: the VALUES matter as much, because a rendering that dropped the keys
#: and left ``"partial"`` sitting in a list would carry exactly the same signal.
FORBIDDEN_IN_PROMPT = ("grade", "sufficient", "partial", "fixture", "status")


def _runbook_sections() -> dict[str, str]:
    """The runbook's ``## `` sections, keyed by heading.

    Chunk text in the stored bundle is exactly ``f"{title} — {section}\\n\\n{body}"``,
    verified against all five captured chunks byte for byte, so reconstructing it here
    gives the tests the REAL retrieved content without a 6KB blob pasted into a test
    file that would then rot away from the corpus.

    Nothing below depends on that format being exactly right — the assertions need
    chunks that carry the bookkeeping fields and real runbook prose, and mild drift in
    the separator costs neither. What they DO depend on is this returning something,
    so a moved or unparseable runbook fails loudly instead of silently handing every
    test an empty fixture to pass against.
    """
    assert RUNBOOK.exists(), f"the runbook fixture moved: {RUNBOOK}"
    body = RUNBOOK.read_text().split("---", 2)[2]
    sections = {
        match.group(1).strip(): match.group(2).strip()
        for match in re.finditer(r"^## (.+?)\n(.*?)(?=^## |\Z)", body, re.M | re.S)
    }
    missing = [name for name in RETRIEVED_SECTIONS if not sections.get(name)]
    assert not missing, f"{RUNBOOK.name} no longer has sections {missing}"
    return sections


def graded_chunks() -> list[dict[str, Any]]:
    """The bundle's ``retrieved_context``, in the shape ``POST /v1/context`` delivers.

    The four prompted fields plus the two that must not survive into the prompt —
    knowledge-service's ``ContextChunk``, with the real values this corpus produces. A
    filter tested against invented bookkeeping proves nothing about the payload that
    actually broke.
    """
    sections = _runbook_sections()
    return [
        {
            "runbook_id": RUNBOOK_ID,
            "title": RUNBOOK_TITLE,
            "section": section,
            "content": f"{RUNBOOK_TITLE} — {section}\n\n{sections[section]}",
            "grade": CHUNK_GRADE,
            "status": CHUNK_STATUS,
        }
        for section in RETRIEVED_SECTIONS
    ]


def bundle_for(chunks: list[dict[str, Any]]) -> ContextBundle:
    """The incident that produced the bug: OrderServiceHighMemory on order-service.

    Metadata and plan steps are the stored ones, so the live counterpart replays the
    real input rather than a plausible-looking substitute.
    """
    return ContextBundle(
        incident_id=UUID("109e2239-b35d-485c-9a56-4a8509f789e7"),
        service_name="order-service",
        alert_name="OrderServiceHighMemory",
        severity=Severity.MEDIUM,
        opened_at=datetime(2026, 7, 26, 8, 16, 33, tzinfo=UTC),
        alert_count=1,
        investigation_steps=[
            PlanStep(
                order=1, description="Check order-service memory trend over last hour"
            ),
            PlanStep(
                order=2, description="Review heap dump or memory profile if available"
            ),
            PlanStep(
                order=3,
                description="Check for memory leak indicators in recent deployments",
            ),
            PlanStep(
                order=4, description="Consider restarting pod if memory is critical"
            ),
        ],
        retrieved_context=chunks,
    )


def test_the_fixture_itself_contains_none_of_the_forbidden_terms() -> None:
    """Which side a failure below is on: the renderer's, or this file's.

    The next assertion is a substring search over rendered JSON, so it is only a
    statement about the projection if the INPUT prose is clean of the same terms. The
    runbook's five sections are — an edit adding "upgrade" or "status code" would
    change that, and this check makes such an edit read as "the fixture is wrong"
    instead of a false accusation against ``render_user_message``.
    """
    for chunk in graded_chunks():
        prose = f"{chunk['title']} {chunk['section']} {chunk['content']}".lower()
        for term in FORBIDDEN_IN_PROMPT:
            assert term not in prose, (
                f"the runbook's {chunk['section']!r} section now contains {term!r}, "
                f"so this fixture can no longer tell a leaked grade apart from the "
                f"excerpt's own words — pick different sections or different terms"
            )


def test_the_rendered_message_carries_none_of_the_grading_bookkeeping() -> None:
    """THE assertion. Grades and review status must not reach the model.

    The field list is a whitelist, so this is also what stops the seventh field the
    context API grows from arriving in the prompt by default.
    """
    rendered = render_user_message(bundle_for(graded_chunks())).lower()

    for term in FORBIDDEN_IN_PROMPT:
        assert term not in rendered, (
            f"{term!r} reached the prompt — the chunk projection is leaking the "
            f"knowledge pipeline's bookkeeping"
        )


def test_the_rendered_message_still_carries_the_excerpt_itself() -> None:
    """The other direction, and it is not a formality.

    A renderer that returned ``"{}"`` — or one that dropped ``retrieved_context``
    while stripping the grades — passes the test above perfectly and destroys
    retrieval completely. That failure is invisible: the RCA quietly goes back to
    generic SRE advice, with a bundle full of the right runbook stored beside it.
    """
    chunks = graded_chunks()
    payload = json.loads(render_user_message(bundle_for(chunks)))

    # Structural, not a substring sweep: the chunk text is multi-line, and asserting
    # over the parsed payload pins the projection EXACTLY — every whitelisted field,
    # its unmodified value, the chunks in order, and nothing else alongside them. A
    # substring check would also pass on a rendering that kept `grade` and happened to
    # contain the excerpt.
    assert payload["retrieved_context"] == [
        {field: chunk[field] for field in PROMPTED_CHUNK_FIELDS} for chunk in chunks
    ]

    # And the non-chunk half goes through untouched: the LIVE severity, the alert
    # name, the plan's steps. Filtering chunks must not cost any of it.
    assert payload["severity"] == "medium"
    assert payload["alert_name"] == "OrderServiceHighMemory"
    assert payload["alert_count"] == 1
    assert payload["service_name"] == "order-service"
    assert [step["description"] for step in payload["investigation_steps"]] == [
        step.description for step in bundle_for(chunks).investigation_steps
    ]


def test_the_system_prompt_says_nothing_about_grades() -> None:
    """The prompt half. It must not instruct the model about fields it cannot see.

    The clause this replaces — "weight excerpts graded sufficient over those graded
    partial" — is what turned every incident's context into self-declared low-quality
    context. Its absence is a property, not a tidy-up, so it is pinned here rather
    than left to whoever next edits the prose.

    The retrieved_context rules that MUST stay are pinned separately, in
    ``test_knowledge.py::test_the_prompt_pins_the_empty_context_rule``.
    """
    prompt = SYSTEM_PROMPT.lower()

    assert "grade" not in prompt, "the prompt still talks about grades"
    assert "sufficient" not in prompt
    assert "partial" not in prompt
    # The rule it must NOT have taken with it on the way out.
    assert "ground your analysis" in prompt


def test_an_empty_context_still_renders_as_an_empty_slot() -> None:
    """The empty case must survive the projection unchanged.

    The prompt's empty-context rule keys off ``retrieved_context`` being empty, and it
    is what makes CRAG's "the corpus does not cover this" verdict produce an honest
    RCA instead of a fabricated runbook. A projection that turned ``[]`` into a
    missing key would break it silently.
    """
    assert '"retrieved_context": []' in render_user_message(bundle_for([]))
