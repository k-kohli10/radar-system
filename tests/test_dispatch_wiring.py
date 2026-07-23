"""feedback-service must be wired into the dispatch config, or every RCA dead-letters.

The reasoner emits ``recommendation.created`` targeting ``feedback-service``. The
outbox worker dispatches it by looking up the target's token in its
``dispatch_tokens`` map, and if the target is absent it fails closed with
``no_dispatch_token`` — PERMANENT, so the event dead-letters immediately, before
any request is made (see the dispatcher's ``no_dispatch_token`` path). Every RCA
would then vanish into the dead-letter queue, and it would look like a Slack
problem when the real cause is a missing entry in a config list.

That list is ``dev-mint-tokens.py``'s ``DISPATCH_TARGETS``: the worker's token map
is rebuilt from exactly it on every run. A service also needs an ``agent_token``
minted (``AGENT_SERVICES``) for that map to have anything to point at. Both are
plain tuples with no runtime check that feedback-service is present — so this test
is that check. It is the same silent-config-join failure mode as
``tests/test_runbook_alert_contract.py``: nothing errors, the pipeline just stops
delivering.

Loaded by path because ``scripts/`` is not an importable package; importing the
module runs only its constant/function definitions (its ``main`` is behind
``if __name__ == "__main__"``), so this has no side effects.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest

_ROOT = Path(__file__).resolve().parent.parent
_MINT_SCRIPT = _ROOT / "scripts" / "dev-mint-tokens.py"


def _load_mint_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("dev_mint_tokens", _MINT_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def mint() -> ModuleType:
    return _load_mint_module()


def test_feedback_service_is_a_dispatch_target(mint: ModuleType) -> None:
    """Without this entry the worker has no token for feedback-service and refuses
    to dispatch recommendation.created — no_dispatch_token, immediate dead-letter."""
    assert "feedback-service" in mint.DISPATCH_TARGETS


def test_feedback_service_has_an_agent_token_minted(mint: ModuleType) -> None:
    """A dispatch target needs an agent token to point the map at; AGENT_SERVICES
    is what gets one minted. In the map but not minted would KeyError the rebuild."""
    assert "feedback-service" in mint.AGENT_SERVICES


def test_every_dispatch_target_has_a_minted_token(mint: ModuleType) -> None:
    """The general invariant behind the two above: the dispatch map is built as
    ``{t: agent_tokens[t] for t in DISPATCH_TARGETS}``, so any target missing from
    AGENT_SERVICES would KeyError at mint time. Pin it for every target, not just
    feedback-service, so a future addition can't reintroduce the gap.
    """
    minted = set(mint.AGENT_SERVICES)
    missing = [t for t in mint.DISPATCH_TARGETS if t not in minted]
    assert missing == [], f"dispatch targets with no minted agent token: {missing}"
