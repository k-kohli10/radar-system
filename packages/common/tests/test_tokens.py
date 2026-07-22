"""The shared token estimator.

Moved here from the gateway's own tests when the estimator was hoisted into
``radar_common``: the gateway ENFORCES input limits and callers PRE-CHECK against
them, so both need the same arithmetic, and a constant that must agree in two
places belongs in neither of them.

The cases below are the gateway's original ones, unchanged — the hoist was a
move, not a rewrite, and these are what pin that.
"""

from __future__ import annotations

import pytest
from radar_common import CHARS_PER_TOKEN, estimate_tokens


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        ([], 0),
        ([""], 0),
        (["abcd"], 1),
        (["abcde"], 2),  # ceil(5/4)
        (["ab", "cd"], 1),  # summed before dividing
        (["x" * 4096 * 4], 4096),
    ],
)
def test_estimate_tokens_is_ceil_of_chars_over_four(
    texts: list[str], expected: int
) -> None:
    assert estimate_tokens(texts) == expected


def test_the_divisor_is_four() -> None:
    """Pinned because two independent callers' agreement depends on this value.

    Changing it is a deliberate decision affecting the gateway's admission
    control and every caller's pre-check at once — which is the point of it
    living in one place.
    """
    assert CHARS_PER_TOKEN == 4


def test_a_single_text_and_a_one_element_iterable_agree() -> None:
    """Embedding limits are per input; chat limits are per conversation.

    Both go through this one function, so a per-input pre-check and the
    gateway's per-input enforcement cannot reach different verdicts.
    """
    text = "x" * 32765

    assert estimate_tokens((text,)) == estimate_tokens([text]) == 8192
