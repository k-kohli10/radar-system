"""Test doubles for the feedback-service suite.

Not a ``test_*`` module, so pytest does not collect it — a helper, in the same
spirit as the worker suite's ``tokens`` and the gateway suite's ``gateway_harness``.

:class:`FakeNotifier` is a fake-that-records standing in for the Slack backend, so
the delivery path is exercised without a real workspace. Because the whole point of
this service is not double-posting, it COUNTS its calls: "how many cards were
posted?" is ``len(notifier.calls)``, which is what the concurrency and idempotency
tests assert on.
"""

from __future__ import annotations

from typing import Any


class FakeNotifier:
    """A recording stand-in for ``NotificationBackend`` (structural conformance).

    Records every ``send`` call so a test can assert the card count and arguments.
    ``ts_values`` supplies a distinct timestamp per call (distinct so a test can
    tell WHICH post recorded, and so concurrent posts get different refs as real
    Slack would). ``fail`` makes ``send`` raise, standing in for any Slack failure —
    used to prove a failed post records nothing and stays retryable.
    """

    def __init__(
        self, *, ts_values: list[str] | None = None, fail: bool = False
    ) -> None:
        self.calls: list[dict[str, Any]] = []
        self._ts_values = list(ts_values or [])
        self._counter = 0
        self._fail = fail

    async def send(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ref: str | None = None,
    ) -> str:
        self.calls.append(
            {
                "channel": channel,
                "text": text,
                "blocks": blocks,
                "thread_ref": thread_ref,
            }
        )
        if self._fail:
            raise RuntimeError("slack unavailable (fake)")
        self._counter += 1
        if self._ts_values:
            return self._ts_values[(self._counter - 1) % len(self._ts_values)]
        return f"1720000000.{self._counter:04d}"
