"""Repo-root pytest configuration: things that must hold for EVERY service's tests.

Deliberately near-empty. This file is on the import path of every test run in the
repository, so anything added here is a global fact about testing RADAR, not a
convenience for one suite. There is exactly one such fact so far, below.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
import structlog


@pytest.fixture(scope="session", autouse=True)
def _structlog_capture_stays_reliable() -> Iterator[None]:
    """Make ``structlog.testing.capture_logs()`` work regardless of test order.

    THE FAILURE THIS PREVENTS

    ``radar_common.configure_logging`` sets ``cache_logger_on_first_use=True`` — correct
    for production, where it saves a rebind on every log call. In a test session it is a
    trap, and the trap has three parts that only bite together:

    1. Modules hold their logger at module scope (``log = get_logger("...")``), which
       creates a lazy proxy but decides nothing.
    2. ``radar_common.bootstrap`` calls ``configure_logging`` on EVERY ``create_app()``,
       so from the first app any test builds, caching is globally on.
    3. A proxy decides whether to cache at its FIRST USE, from the global config at that
       moment. If caching is on, it freezes — structlog shadows the proxy's own ``bind``
       with an instance attribute — and the frozen chain is the JSON-to-stdout one.

    A frozen logger cannot be repaired: ``structlog.configure()`` does not remove that
    instance attribute, and neither does ``reset_defaults()``. So every LATER test that
    calls ``capture_logs()`` on that module's logger sees an EMPTY list while the line
    it is asserting on goes to stdout. The test then reports that a log line was never
    emitted when it demonstrably was.

    That is a nasty failure to debug: it depends on which test used the logger first, so
    it passes in isolation and fails in the suite. And the tests that assert on log
    output are usually the ones guarding "this failure is VISIBLE to an operator" —
    exactly the guarantees that must not quietly stop being checked. The real instance:
    feedback-service asserts an unparseable click surfaces ``interaction.rejected``, and
    it broke the moment an e2e test drove the real handler through that same logger.

    WHY IT PATCHES ``structlog.configure`` INSTEAD OF JUST CALLING IT

    Calling ``configure(cache_logger_on_first_use=False)`` once — or even before every
    test — does not hold: the next ``create_app()`` turns caching straight back on, and
    any logger first used after that freezes. Nothing can then un-freeze it. The flag
    has to be held off for the WHOLE session, so the one place that sets it is wrapped.

    Only that one keyword is overridden; processors, wrapper class and logger factory
    are whatever ``configure_logging`` asked for, so tests still exercise the production
    chain. Nothing here runs outside pytest — production keeps its caching.
    """
    original = structlog.configure

    def _configure_without_logger_caching(**kwargs: Any) -> None:
        kwargs["cache_logger_on_first_use"] = False
        original(**kwargs)

    structlog.configure = _configure_without_logger_caching  # type: ignore[assignment]
    original(cache_logger_on_first_use=False)
    try:
        yield
    finally:
        structlog.configure = original
