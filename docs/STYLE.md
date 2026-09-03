# ✍️ Documentation Style

The house style for every doc in this repo. Match it, and this file itself
is the example. 🎯

---

## Contents

- [The Shape](#-the-shape)
- [Banned](#-banned)
- [Anchors](#-anchors)
- [When to Diagram](#-when-to-diagram)
- [Exceptions](#-exceptions)

---

## 🧭 The Shape

| Element | Rule |
|---|---|
| 🏷️ H1 | One emoji, then the title |
| 📚 Contents | A plain `## Contents` block right after the intro, linking every `##` heading. No emoji in the Contents header or in TOC link text |
| 🎨 Headings | One emoji per `##`/`###`, meaningful (not decorative) |
| 📊 Tables | Prefer a table to a paragraph wherever the content is a list of facts |
| ✅ Steps | Numbered or bulleted, short: one idea per line |
| 💬 Prose | Two to three sentences per paragraph, max |
| 🧩 Code blocks | Short, runnable, only what's needed to make the point |

Reference examples already in this style: [docs/quickstart.md](quickstart.md),
[docs/plugin-development.md](plugin-development.md),
[docs/performance-benchmark.md](performance-benchmark.md).

---

## 🚫 Banned

- ❌ **Em-dashes ("—").** Rewrite the sentence instead of swapping in a dash.
  A colon, a period, or a parenthetical almost always reads better anyway.
- ❌ **Marketing language.** No "seamless," "powerful," "cutting-edge," "robust
  and scalable." State what the thing does.
- ❌ **Dense prose walls.** If a paragraph is explaining a list, make it a list.
- ❌ **Decorative emoji.** Every emoji should help a reader scan, not fill space.
- ❌ **Negative asides that don't inform a decision.** State what something IS;
  don't append "not X" or "never Y" just to name a rejected alternative nobody
  was going to reach for. Keep the contrast only when it heads off a real
  mistake a reader would otherwise make.

  ```
  Bad:  Every one is a method-only, @runtime_checkable typing.Protocol — never an ABC.
  Good: Every one is a method-only, @runtime_checkable typing.Protocol.

  Bad:  Secrets come from Vault, never environment variables.
  Good: (keep this one — env vars ARE the default a reader would reach for,
         so the contrast heads off a real mistake)
  ```
- ❌ **Essay-style "why not X" argumentation in ADRs.** A rejected alternative
  gets one row in a comparison table (what it's for, why it's skipped), not a
  multi-paragraph rebuttal. See ADR 0003 or 0019 for the target shape.

---

## 📐 Anchors

GitHub slugs a heading by lowercasing it, turning spaces into hyphens, and
turning a *leading* emoji + its following space into a *leading* hyphen. So:

```
## 🔍 Troubleshooting   ->   #-troubleshooting
## Contents              ->   #contents
```

Emoji lives only on the real heading, never in the Contents header or the TOC
link text, and the anchor is unaffected: the leading hyphen already encodes
the heading's emoji, so the TOC entry points at it unchanged.

```
## 🎯 Scope        ->   #-scope
- [Scope](#-scope)
```

Check every `## Contents` link resolves before shipping a doc.

---

## 🖼️ When to Diagram

Reach for a small ASCII flow or table over prose whenever you're describing:

- a sequence of steps across systems (e.g. `alert -> ingestion -> watcher -> ...`),
- a before/after or option comparison (use a table),
- a directory or package layout (use a fenced tree).

A diagram earns its place when it answers "what happens, in what order" faster
than a paragraph would. Skip it if the flow is a single, obvious step.

---

## 🔒 Exceptions

Some content is intentionally left alone. Don't apply this style there without
checking first:

- **`docs/runbooks/*.md` body content.** These are RAG source fixtures. Their
  `##` sections (`Summary`, `Symptoms`, `Investigation`, ...) are a literal
  contract with `apps/knowledge-service`'s chunker, content-hashed and
  exact-string-matched by tests. Do not add emoji to their headers, reformat
  their bodies, or edit their prose. `docs/runbooks/README.md` (the index, not
  a runbook) is normal documentation and already follows this style.
- **ADRs (`docs/adr/*.md`)** keep their `## Status / Context / Decision /
  Consequences` structure. Add the emoji H1 and turn comparison/tradeoff lists
  into tables, but don't rename the required sections.
