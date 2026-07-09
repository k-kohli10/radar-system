"""Test harness for the llm-gateway suite.

Builds a fully wired gateway app — real config loading, real token map from
a temp Vault dir, real security/retry/fallback/stream plumbing — with fake
provider plugins so no test ever talks to a vendor. Two sentinel strings are
threaded through every failure path:

- :data:`SECRET_PROMPT` goes into message content;
- :data:`SECRET_VENDOR` is the message of every fake vendor exception.

Log- and response-leak tests assert neither ever appears anywhere.
"""

from __future__ import annotations

import secrets
import textwrap
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient
from prometheus_client import CollectorRegistry
from radar_contracts import (
    GatewayStreamEvent,
    LLMMode,
    LLMRequest,
    LLMResponse,
    Usage,
)
from radar_llm_gateway.api.chat import create_chat_router
from radar_llm_gateway.api.embed import create_embed_router
from radar_llm_gateway.core.config import load_gateway_config, load_token_map
from radar_llm_gateway.core.errors import install_error_handlers
from radar_llm_gateway.core.security import (
    GatewayAuth,
    install_guarded_validation_handler,
)
from radar_llm_gateway.gateway.model_router import ModelRouter
from radar_llm_gateway.gateway.service import GatewayService
from radar_llm_gateway.providers.base import FailureInfo, ProviderBinding
from radar_telemetry import create_llm_metrics

SECRET_PROMPT = "SECRET-PROMPT-CONTENT-NEVER-LOGGED"
SECRET_VENDOR = "SECRET-VENDOR-ERROR-BODY-ECHO"

GATEWAY_YAML = textwrap.dedent(
    """
    modes:
      fast:
        provider: openai
        model: gpt-4o-mini
        max_input_tokens: 4096
        max_output_tokens: 512
        timeout_seconds: 5
      reason:
        provider: openai
        model: gpt-4o
        max_input_tokens: 8192
        max_output_tokens: 2048
        timeout_seconds: 30
      extended:
        provider: openai
        model: gpt-4o
        max_input_tokens: 32768
        max_output_tokens: 8192
        timeout_seconds: 120
      embed:
        provider: openai
        model: text-embedding-3-small
        max_input_tokens: 8191
        timeout_seconds: 10
    fallback:
      extended:
        provider: openai
        model: gpt-4o-mini
      reason:
        provider: openai
        model: gpt-4o-mini
    """
)


class FakeVendorError(Exception):
    """A fake vendor exception whose message must never leak."""

    def __init__(self, status_code: int = 503) -> None:
        super().__init__(SECRET_VENDOR)
        self.status_code = status_code


def translate_fake(exc: BaseException) -> FailureInfo | None:
    if isinstance(exc, FakeVendorError):
        return FailureInfo(status_code=exc.status_code)
    return None


class FakeChat:
    """Scriptable chat provider.

    ``fail_times``/``fail_status`` drive ``complete``; ``stream_scripts`` is
    a queue of per-call scripts for ``stream`` (steps: literal deltas,
    ``"X"`` to raise mid-stream, ``"END"`` to finish with usage). When the
    queue is empty the default happy script runs.
    """

    def __init__(self, model: str) -> None:
        self.model = model
        self.fail_times = 0
        self.fail_status = 503
        self.calls = 0
        self.stream_scripts: list[list[str]] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise FakeVendorError(self.fail_status)
        return LLMResponse(
            id="resp_fake",
            mode=request.mode,
            provider="openai",
            model=self.model,
            content=f"answer from {self.model}",
            usage=Usage(prompt_tokens=10, completion_tokens=4),
            latency_ms=1,
        )

    async def stream(self, request: LLMRequest) -> AsyncIterator[GatewayStreamEvent]:
        self.calls += 1
        script = (
            self.stream_scripts.pop(0)
            if self.stream_scripts
            else ["hello ", "world", "END"]
        )
        for step in script:
            if step == "X":
                raise FakeVendorError(self.fail_status)
            if step == "END":
                yield GatewayStreamEvent(
                    done=True, usage=Usage(prompt_tokens=7, completion_tokens=3)
                )
                return
            yield GatewayStreamEvent(delta=step)


class FakeEmbedder:
    def __init__(self) -> None:
        self.fail_times = 0
        self.calls = 0

    async def embed(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise FakeVendorError(503)
        return [[0.5, 0.25] for _ in texts]


@dataclass
class GatewayEnv:
    """A temp Vault secrets dir plus gateway config on disk."""

    secrets_dir: Path
    config_path: Path
    fast_token: str
    extended_token: str
    embed_token: str


def write_gateway_env(base: Path) -> GatewayEnv:
    fast_token = secrets.token_hex(32)
    extended_token = secrets.token_hex(32)
    embed_token = secrets.token_hex(32)
    (base / "gateway_tokens").write_text(
        "tokens:\n"
        f"  {fast_token}:\n    service: watcher-agent\n    allowed_mode: fast\n"
        f"  {extended_token}:\n"
        "    service: reasoner-agent\n    allowed_mode: extended\n"
        f"  {embed_token}:\n"
        "    service: knowledge-service\n    allowed_mode: embed\n"
    )
    (base / "openai_api_key").write_text("sk-test-not-real\n")
    config_path = base / "gateway.yaml"
    config_path.write_text(GATEWAY_YAML)
    return GatewayEnv(
        secrets_dir=base,
        config_path=config_path,
        fast_token=fast_token,
        extended_token=extended_token,
        embed_token=embed_token,
    )


@dataclass
class GatewayHarness:
    """A wired gateway app over fake providers, plus its control surfaces."""

    env: GatewayEnv
    client: TestClient
    primary_chat: FakeChat
    fallback_chat: FakeChat
    embedder: FakeEmbedder
    metrics_registry: CollectorRegistry
    sleeps: list[float] = field(default_factory=list)

    def metric(self, name: str, **labels: str) -> float | None:
        return self.metrics_registry.get_sample_value(name, labels or None)

    def fast_headers(self) -> dict[str, str]:
        return {"X-Radar-Agent-Token": self.env.fast_token}

    def extended_headers(self) -> dict[str, str]:
        return {"X-Radar-Agent-Token": self.env.extended_token}

    def embed_headers(self) -> dict[str, str]:
        return {"X-Radar-Agent-Token": self.env.embed_token}


def build_harness(env: GatewayEnv) -> GatewayHarness:
    config = load_gateway_config(env.config_path)
    token_map = load_token_map(directory=env.secrets_dir)

    primary_chat = FakeChat("gpt-4o")
    fallback_chat = FakeChat("gpt-4o-mini")
    embedder = FakeEmbedder()

    def chat_binding(chat: FakeChat, timeout: float) -> ProviderBinding:
        return ProviderBinding(
            provider_name="openai",
            model=chat.model,
            timeout_seconds=timeout,
            translate=translate_fake,
            chat=chat,
        )

    primary = {
        LLMMode.FAST: chat_binding(primary_chat, 5),
        LLMMode.REASON: chat_binding(primary_chat, 30),
        LLMMode.EXTENDED: chat_binding(primary_chat, 120),
        LLMMode.EMBED: ProviderBinding(
            provider_name="openai",
            model="text-embedding-3-small",
            timeout_seconds=10,
            translate=translate_fake,
            embedder=embedder,
        ),
    }
    fallback = {
        LLMMode.REASON: chat_binding(fallback_chat, 30),
        LLMMode.EXTENDED: chat_binding(fallback_chat, 120),
    }
    router = ModelRouter(primary, fallback)

    sleeps: list[float] = []

    async def record_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    metrics_registry = CollectorRegistry()
    service = GatewayService(
        config=config,
        router=router,
        metrics=create_llm_metrics(metrics_registry),
        sleep=record_sleep,
    )
    auth = GatewayAuth(token_map)

    app = FastAPI()
    install_error_handlers(app)
    install_guarded_validation_handler(app, auth)
    app.include_router(create_chat_router(service, auth))
    app.include_router(create_embed_router(service, auth))

    return GatewayHarness(
        env=env,
        client=TestClient(app, raise_server_exceptions=False),
        primary_chat=primary_chat,
        fallback_chat=fallback_chat,
        embedder=embedder,
        metrics_registry=metrics_registry,
        sleeps=sleeps,
    )


def chat_body(
    mode: str = "fast", content: str = SECRET_PROMPT, stream: bool = False
) -> dict[str, object]:
    return {
        "mode": mode,
        "messages": [{"role": "user", "content": content}],
        "stream": stream,
    }
