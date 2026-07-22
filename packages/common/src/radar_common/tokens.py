"""Token estimation, shared by everything that has to respect a model's limits.

One divisor, one estimator, one place. The gateway ENFORCES input limits (it
returns 422) and callers PRE-CHECK against the same limits so they can name the
offending input before spending a request. Those two calculations must agree:
if a caller estimates more generously than the gateway, it sends a batch the
gateway rejects; if less, it refuses inputs the gateway would have accepted.

Both used to hold their own copy of the divisor. The drift was benign — the
gateway is the authority either way, so the cost was a worse error message
rather than a wrong result — but a constant that MUST agree in two places is
exactly the duplication this codebase removes on sight, and the person who next
changes one copy has no way to know about the other. Living here makes drift
impossible rather than merely harmless.

The estimate is a heuristic, not a tokenizer. It is deliberately cheap and
deliberately the same everywhere: what matters is that admission control and
pre-checks reach the same verdict, not that either matches a provider's exact
count.
"""

from __future__ import annotations

from collections.abc import Iterable

CHARS_PER_TOKEN = 4
"""Heuristic divisor for estimating tokens from character count."""


def estimate_tokens(texts: Iterable[str]) -> int:
    """Estimate the token count of ``texts`` at ~4 characters per token.

    Takes an iterable rather than a single string because both callers need
    both shapes: chat admission sums a whole conversation, while embedding
    limits are per input and pass a one-element tuple.
    """
    total_chars = sum(len(text) for text in texts)
    return -(-total_chars // CHARS_PER_TOKEN)  # ceil division
