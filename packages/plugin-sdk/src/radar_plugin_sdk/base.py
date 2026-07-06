"""Shared plugin base and metadata types.

RADAR plugins conform to the ``radar-contracts`` backend Protocols
*structurally* — there is deliberately no ABC base class to inherit. This module
holds the descriptive metadata attached to a registration and the SDK's error
hierarchy.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field


class PluginError(Exception):
    """Base class for all plugin SDK errors."""


class PluginNotFoundError(PluginError):
    """Raised when no plugin is registered for a requested protocol and name."""


class PluginConformanceError(PluginError):
    """Raised when a plugin does not implement its declared contract Protocol."""


class PluginMetadata(BaseModel):
    """Descriptive metadata for a registered plugin."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(description="Registration name, e.g. 'openai', 'slack'.")
    version: str = Field(default="0.1.0", description="Plugin version string.")
