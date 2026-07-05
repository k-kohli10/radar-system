"""Configuration models for plugin selection.

A backend is chosen by naming a registered plugin and supplying the keyword
settings passed to its constructor. This is the config the loader turns into a
live backend instance; the concrete settings for each plugin are validated by
that plugin, so ``settings`` is an open mapping here.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class BackendConfig(BaseModel):
    """Selection of a single backend plugin and its constructor settings."""

    model_config = ConfigDict(extra="forbid")

    plugin: str = Field(description="Registered plugin name to load, e.g. 'slack'.")
    settings: dict[str, Any] = Field(
        default_factory=dict,
        description="Keyword settings passed to the plugin constructor.",
    )
