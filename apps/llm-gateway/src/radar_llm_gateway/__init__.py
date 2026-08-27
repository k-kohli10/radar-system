"""RADAR LLM Gateway service.

The single point of LLM access for RADAR: no other service imports a vendor
SDK. Agents authenticate with a static ``X-Radar-Agent-Token`` that maps to
exactly one mode (``fast``, ``reason``, ``extended``, ``embed``); each mode pins
a provider, model, token limits, and timeout in config, so swapping providers is
a config change.

Layout:

- ``api`` - FastAPI routers: ``/v1/complete``, ``/v1/embed``, health, metrics.
- ``core`` - gateway config (modes, tokens, fallback), token IAM, errors.
- ``gateway`` - model router, retry with backoff, provider fallback, streaming,
  and the orchestrating service.
- ``providers`` - thin adapters over the vendor SDKs.

Logging policy: never log message content, API keys, agent tokens, or raw LLM
response bodies. Logged: mode, provider, model, prompt_tokens,
completion_tokens, latency_ms, status_code.
"""

from __future__ import annotations

__version__ = "0.4.0"

__all__ = ["__version__"]
