# 🔌 Plugin Development Guide

Swap in a new backend (an LLM provider, a notification channel, a
logs / metrics / traces sink) **without touching a single service.** 🎉

Every swappable backend in RADAR is just two things:

- 📜 a `Protocol` interface, defined once in `packages/contracts`
- 🧩 a small plugin package that structurally implements it

That's it. No base class, no framework, no service edits.

---

## 📚 Contents

- [🧭 How Plugins Fit](#-how-plugins-fit)
- [📦 Available Protocols](#-available-protocols)
- [🏗️ Anatomy of a Plugin](#-anatomy-of-a-plugin)
- [✍️ Add a New Plugin](#-add-a-new-plugin)
- [🧪 Testing Your Plugin](#-testing-your-plugin)
- [🚫 Rules](#-rules)
- [🔍 Troubleshooting](#-troubleshooting)
- [🧯 Next Steps](#-next-steps)

---

## 🧭 How Plugins Fit

```
radar_contracts            plugins/<category>/<vendor>/       packages/plugin-sdk
──────────────────         ─────────────────────────────      ────────────────────
Protocol interface   <───  structural implementation    <───  PluginRegistry
(zero vendor deps)         (imports the vendor SDK)            + BackendLoader
                                                                       │
                                                                       ▼
                                                          service depends only
                                                          on the Protocol type
```

The flow in one breath:

1. 📜 A category (`LLMProvider`, `NotificationBackend`, …) is defined **once** as a `Protocol`.
2. 🧩 A vendor implements it in its own package under `plugins/<category>/<vendor>/`.
3. 🚀 A service registers that implementation under a **name** at startup.
4. ⚙️ It resolves the backend **from config** at runtime.

📄 The "why" lives in [ADR 0005](adr/0005-plugin-architecture.md).

---

## 📦 Available Protocols

| Protocol | Defined in | Shipped implementation(s) |
|---|---|---|
| `LLMProvider` | `radar_contracts.llm` | `plugins/llm/{openai,anthropic,gemini}` |
| `EmbeddingProvider` | `radar_contracts.knowledge` | `plugins/llm/{openai,gemini}` |
| `KnowledgeStore` | `radar_contracts.knowledge` | `plugins/knowledge/elastic` |
| `NotificationBackend` | `radar_contracts.notifications` | `plugins/notifications/slack` |
| `LogsBackend` | `radar_contracts.logs` | `plugins/logs/elastic` |
| `MetricsBackend` | `radar_contracts.metrics` | `plugins/metrics/prometheus` |
| `TracesBackend` | `radar_contracts.traces` | `plugins/traces/elastic` |

✅ Every one is a method-only, `@runtime_checkable` `typing.Protocol`.

💤 `LogsBackend`, `MetricsBackend`, and `TracesBackend` ship with no service wired
to them yet. That's deliberate build-ahead (see ADR 0005).

---

## 🏗️ Anatomy of a Plugin

```
plugins/<category>/<vendor>/
├── src/radar_plugin_<category>_<vendor>/
│   ├── __init__.py        # re-exports the impl class(es) + PROVIDER/BACKEND name
│   └── <name>.py          # the Protocol implementation
├── tests/
│   └── test_<vendor>_<category>.py
└── pyproject.toml         # depends on radar-contracts + the vendor SDK only
```

📎 **Reference example:** `plugins/llm/openai/`.

- `provider.py` implements both `LLMProvider` and `EmbeddingProvider` over the `openai` SDK.
- Each instance is bound to **one model**.
- Vendor retries are off (`max_retries=0`) so the caller's retry policy is the only one in play.

---

## ✍️ Add a New Plugin

Five steps. The running example: a PagerDuty notification backend. 📟

### 1️⃣ Scaffold the package

Workspace members are `plugins/*/*`, so a new directory with a `pyproject.toml`
**joins the uv workspace automatically**: no root config edit. 🪄

```toml
# plugins/notifications/pagerduty/pyproject.toml
[project]
name = "radar-plugin-notifications-pagerduty"
version = "0.1.0"
dependencies = ["radar-contracts", "pdpyras==5.2.0"]

[tool.uv.sources]
radar-contracts = { workspace = true }
```

### 2️⃣ Implement the protocol

Write a **plain class** (nothing to inherit) whose method signatures
structurally match the target `Protocol`.

> 💡 Let vendor exceptions propagate unwrapped. Classification, redaction, and
> retries are the caller's job, not the plugin's.

### 3️⃣ Register it

In the consuming service's composition root, e.g. `register_plugins()` in
[`apps/llm-gateway/…/main.py`](../apps/llm-gateway/src/radar_llm_gateway/main.py):

```python
registry.register(NotificationBackend, PagerDutyBackend, name="pagerduty")
```

🛡️ Registration runs `issubclass(impl, protocol)` immediately and raises
`PluginConformanceError` on a mismatch: a wiring bug fails **at startup**, not
in production.

### 4️⃣ Point config at the name

`BackendLoader.load()` turns a `BackendConfig` into a live instance: it looks up
the name and calls the class with `settings` as keyword arguments. ⚙️

```yaml
plugin: pagerduty
settings:
  routing_key: ...
```

### 5️⃣ Test it

👉 See the next section.

---

## 🧪 Testing Your Plugin

| Test | What it proves | Reference |
|---|---|---|
| ✅ **Conformance** | Registering against the `Protocol` succeeds: a real structural match, not just "it imports" | any plugin's tests |
| 🎭 **Behavior** | Methods do the right thing against a **mocked vendor client** | `plugins/notifications/slack/tests/` builds the real `SlackNotificationBackend` |

Run just this package:

```bash
cd plugins/<category>/<vendor> && uv run pytest
```

Run the whole suite + gates:

```bash
make test      # full test suite
make lint      # ruff + mypy strict — catches a signature that's close but not conformant
```

---

## 🚫 Rules

- 🔒 **No vendor imports outside `plugins/`.** `apps/` and `packages/` see only
  `radar_contracts` Protocol types. Need a vendor SDK in a service? The
  abstraction is wrong: fix the Protocol, don't reach around it.
- 🧬 **Structural, not nominal.** Don't inherit the Protocol, don't add an ABC.
  Conformance is `issubclass` against a `@runtime_checkable` Protocol.
- 🏷️ **One name per protocol.** `registry.register()` raises on a duplicate: pick
  a distinct vendor name (`"pagerduty"`, not `"slack"`).
- ⚙️ **Config, not code, picks the backend.** A deployment's choice is a
  `BackendConfig` value (YAML), never a hardcoded class in a service.
- 🔑 **Secrets from Vault only.** API keys and tokens come from Vault secret
  files: never environment variables, never the config YAML.

---

## 🔍 Troubleshooting

| 😖 Symptom | 🤔 Cause | 🔧 Fix |
|---|---|---|
| `PluginConformanceError` at registration | Signature doesn't structurally match the `Protocol` | Diff against the `Protocol` in `radar_contracts`: check param names and async/sync |
| `PluginConformanceError`: name already registered | Two plugins share a name for the same `Protocol` | Give the new plugin a distinct `name=` |
| `PluginNotFoundError` at load time | Config's `plugin` name matches no `register(..., name=…)` call | Line up the config value with `register_plugins()` |
| mypy strict fails in CI, not locally | Vendor stubs differ, or the SDK version isn't pinned | Pin the exact vendor SDK version used elsewhere in the repo |

---

## 🧯 Next Steps

| Go deeper | Where |
|---|---|
| 📜 Protocol definitions | [`packages/contracts/…/radar_contracts/`](../packages/contracts/src/radar_contracts/) |
| 🧰 Registry & loader | [`packages/plugin-sdk/…/radar_plugin_sdk/`](../packages/plugin-sdk/src/radar_plugin_sdk/) |
| 🧠 Why this shape | [ADR 0005](adr/0005-plugin-architecture.md) |
| 📎 A full worked example | [`plugins/llm/openai/`](../plugins/llm/openai/) |
