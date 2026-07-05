"""RADAR plugin SDK.

Base types, a protocol-conformance registry, and a config-driven loader for the
backend plugins in ``plugins/*``.

Hard rules for this package:

- Zero vendor imports. Depends only on ``radar-contracts``, ``pydantic``, and
  the standard library.
- Plugins conform to the ``radar-contracts`` backend Protocols structurally
  (``typing.Protocol``), never by ABC inheritance.
- mypy strict must pass.

Consumers import from the top level::

    from radar_plugin_sdk import PluginRegistry, BackendLoader, BackendConfig
"""

from __future__ import annotations

from .base import (
    PluginConformanceError,
    PluginError,
    PluginMetadata,
    PluginNotFoundError,
)
from .registry import PluginRegistry

__version__ = "0.2.0"

__all__ = [
    # base
    "PluginError",
    "PluginNotFoundError",
    "PluginConformanceError",
    "PluginMetadata",
    # registry
    "PluginRegistry",
]
