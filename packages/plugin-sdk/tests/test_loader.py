"""Config-driven loader tests."""

from __future__ import annotations

from typing import Any

import pytest
from radar_contracts import NotificationBackend
from radar_plugin_sdk import (
    BackendConfig,
    BackendLoader,
    PluginNotFoundError,
    PluginRegistry,
)


class RecordingNotifier:
    def __init__(self, token: str = "default") -> None:
        self.token = token

    async def send(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ref: str | None = None,
    ) -> str:
        return f"{self.token}:{channel}"

    async def update(
        self,
        channel: str,
        message_ref: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
    ) -> None: ...


def _loader() -> BackendLoader:
    reg = PluginRegistry()
    reg.register(NotificationBackend, RecordingNotifier, name="slack")
    return BackendLoader(reg)


def test_load_uses_config_settings() -> None:
    loader = _loader()
    config = BackendConfig(plugin="slack", settings={"token": "xoxb-1"})
    backend = loader.load(NotificationBackend, config)
    assert isinstance(backend, RecordingNotifier)
    assert backend.token == "xoxb-1"


def test_load_applies_default_settings_when_absent() -> None:
    loader = _loader()
    backend = loader.load(NotificationBackend, BackendConfig(plugin="slack"))
    assert isinstance(backend, RecordingNotifier)
    assert backend.token == "default"


def test_load_from_dict_validates_and_instantiates() -> None:
    loader = _loader()
    backend = loader.load_from_dict(
        NotificationBackend,
        {"plugin": "slack", "settings": {"token": "xoxb-2"}},
    )
    assert isinstance(backend, RecordingNotifier)
    assert backend.token == "xoxb-2"


def test_load_unknown_plugin_raises_not_found() -> None:
    loader = _loader()
    with pytest.raises(PluginNotFoundError):
        loader.load(NotificationBackend, BackendConfig(plugin="pagerduty"))
