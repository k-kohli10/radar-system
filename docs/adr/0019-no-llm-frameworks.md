# 🧱 ADR 0019: No LangChain, LangGraph, or LiteLLM

> Renumbered from ADR 0004 when this was moved out of
> `docs/implementation_plan.md`. ADR 0004 was already taken by
> [0004-llm-gateway.md](0004-llm-gateway.md), a different decision.

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap Kohli

---

## Contents

- [Context](#-context)
- [Decision](#-decision)
- [Comparison](#-comparison)
- [What We Do Instead](#-what-we-do-instead)
- [Tradeoffs Accepted](#-tradeoffs-accepted)
- [Decision Record](#-decision-record)

---

## 🧭 Context

LangChain, LangGraph, and LiteLLM are the common defaults for calling LLMs. This
ADR records why RADAR uses none of them.

---

## ⚖️ Decision

RADAR calls LLM provider SDKs directly through a custom gateway, with no
orchestration framework and no abstraction layer beyond what the gateway itself
provides.

---

## 🔀 Comparison

| | What it's for | Why RADAR skips it |
|---|---|---|
| **LangChain** | Chains, agents, memory, tools, and a large integration ecosystem on top of LLM calls | The wrapping adds indirection to debug through, a fast-moving API surface to track, and dozens of transitive dependencies. RADAR's gateway has three: the Anthropic, OpenAI, and Gemini SDKs |
| **LangGraph** | Modeling agent pipelines as graphs | RADAR's pipeline is linear (Watcher → Planner → Reasoner), and the Postgres outbox is already its orchestration layer. A graph framework on top would be a second system to keep in sync with it |
| **LiteLLM** | A unified API across LLM providers | The gateway already provides this: providers are plugins, and swapping one is a config change |

---

## 🧰 What We Do Instead

The LLM gateway is raw Python:

- Each provider has one file, implementing the `LLMProvider` protocol from
  `packages/contracts`
- The gateway routes requests to the right provider based on mode config
- Retry, timeout, fallback, and audit logging are implemented once, in the gateway

---

## ⚠️ Tradeoffs Accepted

| Tradeoff | Cost | Why it's acceptable |
|---|---|---|
| More code to write upfront | Writing provider adapters from scratch takes longer than installing LangChain | One-time cost that pays off every time you debug something in production |
| No ecosystem integrations | LangChain has integrations with hundreds of tools; RADAR does not need them | If it ever does, writing a specific integration is less risky than pulling in the entire ecosystem |
| Manual updates when provider SDKs change | When Anthropic releases a breaking change, you update `anthropic_provider.py` directly | With LangChain you'd wait for their wrapper update, then bump your LangChain version. The manual path is faster and more predictable |

---

## ✔️ Decision Record

No LangChain. No LangGraph. No LiteLLM. Raw Python with direct SDK calls through
a custom gateway. This decision does not get revisited unless RADAR needs to support
20+ providers simultaneously, which is not a v1, v2, or likely v3 requirement.
