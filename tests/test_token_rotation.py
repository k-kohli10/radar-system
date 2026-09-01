"""Two-phase agent-token rotation: the outgoing token stays accepted mid-roll.

``make rotate`` mints a new ``agent_token`` but keeps the old one under
``agent_token_previous``, which every service accepts alongside its current token
(see ``radar_common.bootstrap.load_agent_tokens``). Without that, the window
between the target and worker pods restarting dead-letters events: the worker sends
a token the target has already stopped accepting, and a 401 is permanent — immediate
dead-letter, never retried. ``--finalize`` clears the previous token once the roll
has converged.

These pin the PROVISIONING half (the script). The ACCEPTANCE half — a target
verifying both tokens — is pinned in ``packages/common/tests/test_bootstrap.py``.

The script is loaded by path (``scripts/`` is not an importable package); importing
it runs only its constant/function definitions, its ``main`` being behind
``if __name__ == "__main__"``.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType
from typing import Any

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


class _FakeVault:
    """In-memory stand-in for the script's Vault: one data dict per path."""

    def __init__(self, initial: dict[str, dict[str, Any]] | None = None) -> None:
        self._store: dict[str, dict[str, Any]] = {
            path: dict(data) for path, data in (initial or {}).items()
        }

    def read(self, path: str) -> dict[str, Any]:
        return dict(self._store.get(path, {}))

    def write(self, path: str, data: dict[str, Any]) -> None:
        self._store[path] = dict(data)


def test_rotate_keeps_the_outgoing_token_as_previous(mint: ModuleType) -> None:
    """Rotating moves the current token to agent_token_previous and mints a new one.

    Mutation guard: if the rotate branch stops writing ``agent_token_previous``, the
    old token is dropped instantly and a mid-roll dispatch 401s → dead-letter.
    """
    service = "watcher-agent"
    path = mint.service_path(service)
    vault = _FakeVault({path: {"agent_token": "OLD"}})

    mint.mint_agent_tokens(vault, rotate=service)

    secret = vault.read(path)
    assert secret["agent_token_previous"] == "OLD"
    assert secret["agent_token"] != "OLD"
    assert secret["agent_token"]  # a fresh token was minted


def test_initial_mint_writes_no_previous(mint: ModuleType) -> None:
    """A first mint (no existing token) must not invent a previous token."""
    service = "watcher-agent"
    path = mint.service_path(service)
    vault = _FakeVault()

    mint.mint_agent_tokens(vault, rotate=None)

    secret = vault.read(path)
    assert secret["agent_token"]
    assert "agent_token_previous" not in secret


def test_non_rotated_service_is_untouched_by_a_rotation(mint: ModuleType) -> None:
    """Rotating one service leaves another's secret exactly as it was — and never
    gives it a spurious previous token."""
    keep_path = mint.service_path("planner-agent")
    vault = _FakeVault(
        {
            keep_path: {"agent_token": "KEEP"},
            mint.service_path("watcher-agent"): {"agent_token": "OLD"},
        }
    )

    mint.mint_agent_tokens(vault, rotate="watcher-agent")

    assert vault.read(keep_path) == {"agent_token": "KEEP"}


def test_finalize_clears_the_previous_token(mint: ModuleType) -> None:
    """--finalize drops agent_token_previous so the retired token stops being
    accepted, leaving the current token intact."""
    service = "watcher-agent"
    path = mint.service_path(service)
    vault = _FakeVault({path: {"agent_token": "NEW", "agent_token_previous": "OLD"}})

    mint.finalize_agent_rotation(vault, service)

    secret = vault.read(path)
    assert "agent_token_previous" not in secret
    assert secret["agent_token"] == "NEW"


def test_finalize_is_a_noop_without_a_previous_token(mint: ModuleType) -> None:
    service = "watcher-agent"
    path = mint.service_path(service)
    vault = _FakeVault({path: {"agent_token": "NEW"}})

    mint.finalize_agent_rotation(vault, service)  # must not raise

    assert vault.read(path) == {"agent_token": "NEW"}
