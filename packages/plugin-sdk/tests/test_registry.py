"""Registry protocol-conformance tests."""

from __future__ import annotations

from typing import Any, Protocol

import pytest
from radar_contracts import LLMProvider, NotificationBackend
from radar_plugin_sdk import (
    PluginConformanceError,
    PluginNotFoundError,
    PluginRegistry,
)


class GoodNotifier:
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


class AnotherNotifier:
    async def send(
        self,
        channel: str,
        text: str,
        *,
        blocks: list[dict[str, Any]] | None = None,
        thread_ref: str | None = None,
    ) -> str:
        return "ok"


class BadNotifier:
    """Missing ``send``; does not implement NotificationBackend."""

    def notify(self) -> None: ...


def test_register_and_get_conforming_plugin() -> None:
    reg = PluginRegistry()
    reg.register(NotificationBackend, GoodNotifier, name="slack", version="1.0.0")
    assert reg.get(NotificationBackend, "slack") is GoodNotifier


def test_create_instantiates_with_settings() -> None:
    reg = PluginRegistry()
    reg.register(NotificationBackend, GoodNotifier, name="slack")
    instance = reg.create(NotificationBackend, "slack", token="xoxb")
    assert isinstance(instance, GoodNotifier)
    assert instance.token == "xoxb"


def test_register_rejects_nonconforming_plugin() -> None:
    reg = PluginRegistry()
    with pytest.raises(PluginConformanceError):
        # Conformance is enforced at runtime; mypy widens the shared TypeVar
        # rather than rejecting a non-conforming impl here.
        reg.register(NotificationBackend, BadNotifier, name="broken")


def test_register_rejects_duplicate_name() -> None:
    reg = PluginRegistry()
    reg.register(NotificationBackend, GoodNotifier, name="slack")
    with pytest.raises(PluginConformanceError):
        reg.register(NotificationBackend, AnotherNotifier, name="slack")


def test_same_name_allowed_for_different_protocols() -> None:
    reg = PluginRegistry()
    reg.register(NotificationBackend, GoodNotifier, name="dup")

    class LLMImpl:
        async def complete(self, request: Any) -> Any: ...  # pragma: no cover
        def stream(self, request: Any) -> Any: ...  # pragma: no cover

    reg.register(LLMProvider, LLMImpl, name="dup")
    assert reg.get(LLMProvider, "dup") is LLMImpl
    assert reg.get(NotificationBackend, "dup") is GoodNotifier


def test_get_unknown_plugin_raises_not_found() -> None:
    reg = PluginRegistry()
    with pytest.raises(PluginNotFoundError):
        reg.get(NotificationBackend, "missing")


def test_names_are_sorted_and_scoped_to_protocol() -> None:
    reg = PluginRegistry()
    reg.register(NotificationBackend, AnotherNotifier, name="zulip")
    reg.register(NotificationBackend, GoodNotifier, name="slack")
    assert reg.names(NotificationBackend) == ["slack", "zulip"]
    assert reg.names(LLMProvider) == []


def test_metadata_records_name_and_version() -> None:
    reg = PluginRegistry()
    reg.register(NotificationBackend, GoodNotifier, name="slack", version="2.1.0")
    meta = reg.metadata(NotificationBackend, "slack")
    assert meta.name == "slack"
    assert meta.version == "2.1.0"


def test_metadata_unknown_plugin_raises_not_found() -> None:
    reg = PluginRegistry()
    with pytest.raises(PluginNotFoundError):
        reg.metadata(NotificationBackend, "missing")


def test_non_runtime_checkable_protocol_is_rejected() -> None:
    class PlainProtocol(Protocol):
        def do(self) -> None: ...

    class Impl:
        def do(self) -> None: ...

    reg = PluginRegistry()
    with pytest.raises(PluginConformanceError):
        reg.register(PlainProtocol, Impl, name="p")
