# 📓 Changelog

Format follows [Keep a Changelog](https://keepachangelog.com). For milestone
history, tags, and what's next, see [docs/roadmap.md](docs/roadmap.md); for the
full technical plan, see [docs/implementation_plan.md](docs/implementation_plan.md).

---

## [Unreleased]

---

## [1.0.0] - 2026-09-01

RADAR's first public release.

### ✨ Added
- ⏱️ **15-minute quickstart**: clone to a live, LLM-generated RCA on one machine ([docs/quickstart.md](docs/quickstart.md)).
- 🔌 **Plugin development guide**: add an LLM, notification, logs, metrics, or traces backend without touching a service ([docs/plugin-development.md](docs/plugin-development.md)).
- 📊 **Performance benchmark**: a 100-alert burst on a live Kubernetes cluster with the real LLM in the loop, zero data loss ([docs/performance-benchmark.md](docs/performance-benchmark.md)).
- 📓 **This changelog** and a refreshed **contributor guide** ([CONTRIBUTING.md](CONTRIBUTING.md)).
- ✍️ **A house documentation style** applied across every ADR, operations doc, architecture doc, and package/app README ([docs/STYLE.md](docs/STYLE.md)).
- 🖼️ **Project logo and an embedded whole-system architecture schematic** in the README, with a link to the interactive version.
- 🛡️ **Supply-chain security CI**: an OSV-Scanner dependency vulnerability workflow and an OpenSSF Scorecard workflow, each with a README badge.

### 💅 Changed
- 📖 **README** polished: run paths, stack, badges, a Contributing section, and honest status.
- 🧹 **Trimmed oversized code comments** across every service, shared package, and plugin, dropping narrative history while keeping every tested invariant.

---

## 🏷️ Versioning

Milestones are the version scheme (`v0.x-<focus>`). `v1.0.0`, dated
2026-09-01, is RADAR's first public release. See
[docs/roadmap.md](docs/roadmap.md) for the full milestone history.
