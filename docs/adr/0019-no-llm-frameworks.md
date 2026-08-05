# ADR 0019: No LangChain, LangGraph, or LiteLLM

> Renumbered from ADR 0004 when this was moved out of
> `docs/implementation_plan.md`. ADR 0004 was already taken by
> [0004-llm-gateway.md](0004-llm-gateway.md), a different decision.

**Status**: Accepted
**Date**: 2025-01-15
**Author**: Kashyap

---

## Context

Building an AI system that calls LLMs means you will encounter LangChain, LangGraph,
and LiteLLM within five minutes of googling. They are popular, widely used, and
constantly recommended. This ADR explains why RADAR uses none of them.

---

## Decision

RADAR calls LLM provider SDKs directly through a custom gateway. No orchestration
framework. No abstraction layer beyond what we write ourselves.

---

## Why Not LangChain

LangChain is an abstraction layer on top of LLM calls that also includes chains,
agents, memory, tools, retrievers, and a large ecosystem of integrations.

The problems:

**It abstracts the wrong things.** LangChain wraps the LLM call itself in layers
of indirection. When something breaks, you are debugging LangChain internals, not
your code. The stack traces are long, the error messages are vague, and the source
of truth is spread across multiple abstraction layers.

**The abstraction leaks constantly.** Every non-trivial use case requires dropping
down to provider-specific behavior anyway. At that point you are fighting the
abstraction rather than using it.

**It changes rapidly and breaks things.** LangChain has an aggressive release
cadence with frequent breaking changes. Pinning a version works until you need a
bug fix, then you upgrade and spend a week fixing breakage. For a platform that
needs to be stable and debuggable at 3am, this is a real risk.

**It adds a supply chain dependency.** LangChain pulls in dozens of transitive
dependencies. Each one is a potential vulnerability, a version conflict, or a
source of unexpected behavior. RADAR's LLM gateway has three dependencies: the
Anthropic SDK, the OpenAI SDK, and the Gemini SDK. That is it.

**It does not fit the architecture.** RADAR has a custom gateway with mode-based
IAM, per-mode timeouts, fallback providers, and audit logging. Implementing this
correctly inside LangChain is harder than implementing it without LangChain. The
framework would be fighting the design.

**It hides what you are actually doing.** For a portfolio project and a future
open-source tool, the code needs to be readable and understandable by someone who
has never seen it before. LangChain code is not readable to someone unfamiliar with
its abstractions. Raw Python calling an SDK is.

---

## Why Not LangGraph

LangGraph is LangChain's answer to agent orchestration. It models agent pipelines
as graphs with nodes and edges.

The additional problems:

**RADAR's pipeline is linear.** Watcher -> Planner -> Reasoner. It is not a graph.
Using a graph framework to model a linear pipeline is using a sledgehammer on a nail.

**The Postgres outbox is already the orchestration layer.** Events flow between
agents via the outbox. The outbox is the graph. Adding LangGraph on top means two
orchestration systems that need to stay in sync.

**Debugging graph-based agents is harder than debugging sequential code.** When
Planner fails, you want to look at a log line and understand exactly what happened.
With LangGraph you are looking at graph traversal state and trying to reconstruct
what the framework decided to do.

---

## Why Not LiteLLM

LiteLLM is a proxy/abstraction layer that provides a unified API across LLM
providers. The argument for it is that you write one integration and get all
providers for free.

The problems:

**RADAR already has this.** The LLM gateway provides a unified internal API.
Providers are plugins. Swapping providers is a config change. LiteLLM solves a
problem that is already solved, just differently.

**It is another service or library to depend on.** LiteLLM as a proxy means another
deployment to manage. LiteLLM as a library means another set of transitive
dependencies and another thing that can break.

**Supply chain risk.** LiteLLM has had documented security issues in the past.
For a system that handles operational alerts and calls external APIs with real
credentials, supply chain risk is not theoretical. Fewer dependencies means a
smaller attack surface.

**It abstracts provider-specific behavior.** Each provider has quirks: different
token counting methods, different streaming formats, different error codes, different
retry behavior. LiteLLM normalizes these, which sounds good until you need to debug
why your Gemini calls are failing differently from your OpenAI calls. The abstraction
hides information you need.

**Loss of control over the gateway.** RADAR's gateway enforces mode-based IAM,
per-mode token limits, specific timeouts, and audit logging. Doing this cleanly
through LiteLLM is harder than doing it directly. You end up wrapping LiteLLM in
your own layer, which defeats the point.

---

## What We Do Instead

The LLM gateway is raw Python:

- Each provider has one file: `anthropic_provider.py`, `openai_provider.py`,
  `gemini_provider.py`
- Each implements the `LLMProvider` protocol from `packages/contracts`
- The gateway routes requests to the right provider based on mode config
- Retry, timeout, fallback, and audit logging are implemented once in the gateway
- The whole thing is under 500 lines of code and readable by anyone who knows Python

When something breaks, you read the code. There is no framework to understand first.

---

## Tradeoffs Accepted

**More code to write upfront.** Writing provider adapters from scratch takes longer
than installing LangChain. This is a one-time cost that pays off every time you
debug something in production.

**No ecosystem integrations.** LangChain has integrations with hundreds of tools.
RADAR does not need them. If it ever does, writing a specific integration is less
risky than pulling in the entire ecosystem.

**Manual updates when provider SDKs change.** When Anthropic releases a breaking
change, you update `anthropic_provider.py`. With LangChain you wait for LangChain
to update their wrapper and then update your LangChain version. The manual path
is faster and more predictable.

---

## Decision Record

No LangChain. No LangGraph. No LiteLLM. Raw Python with direct SDK calls through
a custom gateway. This decision does not get revisited unless RADAR needs to support
20+ providers simultaneously, which is not a v1, v2, or likely v3 requirement.
