"""Shared dispatch-token maps for the worker suites.

Not a ``test_*`` module, so pytest does not collect it — it is a helper, in the
same spirit as the gateway suite's ``gateway_harness``.

Most of these suites are not *about* authentication: they exercise polling,
dead-lettering, and shutdown, and merely need a dispatcher that can authenticate to
its target. They get :func:`token_map` for that. The suite that genuinely is about
the tokens (``test_dispatch_envelope``) builds its own maps with distinct values per
target, because "sends the right one" is only provable when the wrong one is
distinguishable.
"""

from __future__ import annotations

import hashlib

from radar_outbox_worker.security import DispatchTokenMap

DEFAULT_TARGET = "watcher-agent"
"""The target every worker suite dispatches to unless it says otherwise."""


def token_map(*targets: str) -> DispatchTokenMap:
    """A dispatch map granting a distinct 64-hex token to each named target.

    Distinct per target even here — a map whose values were all identical would let
    a "sends the target's token" bug pass unnoticed in any suite that happened to
    reuse it.
    """
    names = targets or (DEFAULT_TARGET,)
    return DispatchTokenMap({name: token_for(name) for name in names})


def token_for(target: str) -> str:
    """The deterministic 64-hex test token belonging to ``target``.

    Derived from the name (sha256, not ``hash()`` — which is seed-randomized across
    processes) so a test can assert WHICH token went out without the map and the
    assertion having to agree on a literal.
    """
    return hashlib.sha256(target.encode()).hexdigest()
