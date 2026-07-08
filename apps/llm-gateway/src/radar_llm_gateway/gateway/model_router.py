"""Mode → provider routing.

:func:`build_router` turns the validated :class:`GatewayConfig` into a
:class:`ModelRouter` holding one live :class:`ProviderBinding` per mode
(primary) plus one per configured fallback entry. All construction happens at
startup so a bad config — unknown vendor, unregistered plugin, missing API key
— fails readiness loudly instead of failing the first request.

Provider plugins are instantiated through the plugin SDK's
:class:`BackendLoader`, so the router never imports a vendor SDK; the vendor
specifics it needs (Vault secret name for the API key, exception translator)
come from the :data:`VENDORS` table assembled from ``providers/*``.

Fallback bindings reuse the mode's own ``timeout_seconds`` and
``max_output_tokens``: the fallback config names only a provider and model,
and a mode's limits are the mode's limits regardless of which provider serves
it. Embed modes get an ``EmbeddingProvider`` plugin; chat modes an
``LLMProvider``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from pydantic import SecretStr
from radar_common import ConfigurationError, read_secret
from radar_contracts import EmbeddingProvider, LLMMode, LLMProvider
from radar_plugin_sdk import BackendConfig, BackendLoader, PluginError, PluginRegistry

from radar_llm_gateway.core.config import GatewayConfig
from radar_llm_gateway.providers import (
    anthropic_provider,
    gemini_provider,
    openai_provider,
)
from radar_llm_gateway.providers.base import FailureTranslator, ProviderBinding


@dataclass(frozen=True)
class VendorSpec:
    """The gateway-side description of one LLM vendor."""

    name: str
    api_key_secret: str
    translate: FailureTranslator


VENDORS: Mapping[str, VendorSpec] = {
    spec.name: spec
    for spec in (
        VendorSpec(
            name=openai_provider.PROVIDER_NAME,
            api_key_secret=openai_provider.API_KEY_SECRET,
            translate=openai_provider.translate_failure,
        ),
        VendorSpec(
            name=anthropic_provider.PROVIDER_NAME,
            api_key_secret=anthropic_provider.API_KEY_SECRET,
            translate=anthropic_provider.translate_failure,
        ),
        VendorSpec(
            name=gemini_provider.PROVIDER_NAME,
            api_key_secret=gemini_provider.API_KEY_SECRET,
            translate=gemini_provider.translate_failure,
        ),
    )
}
"""Every vendor the gateway can route to, keyed by config ``provider:`` name."""


class ModelRouter:
    """Resolved bindings: which live provider serves each mode."""

    def __init__(
        self,
        primary: Mapping[LLMMode, ProviderBinding],
        fallback: Mapping[LLMMode, ProviderBinding],
    ) -> None:
        self._primary = dict(primary)
        self._fallback = dict(fallback)

    def primary_for(self, mode: LLMMode) -> ProviderBinding:
        """The binding configured for ``mode`` (every mode has one)."""
        return self._primary[mode]

    def fallback_for(self, mode: LLMMode) -> ProviderBinding | None:
        """The fallback binding for ``mode``, or None if not configured."""
        return self._fallback.get(mode)


def build_router(
    config: GatewayConfig,
    registry: PluginRegistry,
    *,
    secrets_directory: Path | None = None,
) -> ModelRouter:
    """Construct every binding the config calls for; fail startup on any gap.

    API keys are read from Vault once per *referenced* vendor — a vendor no
    mode routes to needs no key on disk.
    """
    loader = BackendLoader(registry)
    api_keys: dict[str, SecretStr] = {}

    def api_key(vendor: VendorSpec) -> SecretStr:
        if vendor.name not in api_keys:
            secret = read_secret(vendor.api_key_secret, directory=secrets_directory)
            assert secret is not None  # required=True: read_secret raised if absent
            api_keys[vendor.name] = secret
        return api_keys[vendor.name]

    def vendor_for(provider: str, where: str) -> VendorSpec:
        try:
            return VENDORS[provider]
        except KeyError:
            known = ", ".join(sorted(VENDORS))
            raise ConfigurationError(
                f"{where}: unknown provider '{provider}' (known: {known})"
            ) from None

    def build_binding(
        provider: str,
        model: str,
        *,
        timeout_seconds: float,
        max_output_tokens: int | None,
        embed: bool,
        where: str,
    ) -> ProviderBinding:
        vendor = vendor_for(provider, where)
        settings: dict[str, object] = {
            "model": model,
            "api_key": api_key(vendor).get_secret_value(),
        }
        try:
            if embed:
                embedder = loader.load(
                    EmbeddingProvider,
                    BackendConfig(plugin=vendor.name, settings=settings),
                )
                return ProviderBinding(
                    provider_name=vendor.name,
                    model=model,
                    timeout_seconds=timeout_seconds,
                    translate=vendor.translate,
                    embedder=embedder,
                )
            settings["max_output_tokens"] = max_output_tokens
            chat = loader.load(
                LLMProvider, BackendConfig(plugin=vendor.name, settings=settings)
            )
            return ProviderBinding(
                provider_name=vendor.name,
                model=model,
                timeout_seconds=timeout_seconds,
                translate=vendor.translate,
                chat=chat,
            )
        except PluginError as exc:
            # PluginError messages carry registry names only, never settings.
            raise ConfigurationError(f"{where}: {exc}") from None

    primary: dict[LLMMode, ProviderBinding] = {}
    fallback: dict[LLMMode, ProviderBinding] = {}

    for mode, mode_cfg in config.modes.items():
        is_embed = mode is LLMMode.EMBED
        primary[mode] = build_binding(
            mode_cfg.provider,
            mode_cfg.model,
            timeout_seconds=mode_cfg.timeout_seconds,
            max_output_tokens=mode_cfg.max_output_tokens,
            embed=is_embed,
            where=f"mode '{mode.value}'",
        )
        fallback_cfg = config.fallback.get(mode)
        if fallback_cfg is not None:
            fallback[mode] = build_binding(
                fallback_cfg.provider,
                fallback_cfg.model,
                timeout_seconds=mode_cfg.timeout_seconds,
                max_output_tokens=mode_cfg.max_output_tokens,
                embed=is_embed,
                where=f"fallback for mode '{mode.value}'",
            )

    return ModelRouter(primary, fallback)
