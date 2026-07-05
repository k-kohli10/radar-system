"""Config-driven backend loader.

``BackendLoader`` turns a ``BackendConfig`` into a live backend instance by
looking the named plugin up in a ``PluginRegistry`` and instantiating it with
the configured settings. The registry enforces protocol conformance, so a
loaded backend is guaranteed to satisfy ``protocol``.
"""

from __future__ import annotations

from typing import Any, TypeVar

from .config import BackendConfig
from .registry import PluginRegistry

T = TypeVar("T")


class BackendLoader:
    """Instantiate backend plugins from configuration."""

    def __init__(self, registry: PluginRegistry) -> None:
        self._registry = registry

    def load(self, protocol: type[T], config: BackendConfig) -> T:
        """Instantiate the plugin named by ``config`` as an implementer of ``protocol``.

        Raises ``PluginNotFoundError`` if no plugin is registered under
        ``config.plugin`` for ``protocol``.
        """
        return self._registry.create(protocol, config.plugin, **config.settings)

    def load_from_dict(self, protocol: type[T], data: dict[str, Any]) -> T:
        """Validate raw config (e.g. parsed YAML) into ``BackendConfig``, then load."""
        return self.load(protocol, BackendConfig.model_validate(data))
