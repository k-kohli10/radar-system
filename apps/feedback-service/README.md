# 💬 radar-feedback-service

The last stage of the RADAR incident pipeline, and the only one an engineer sees.

Consumes `recommendation.created` from the outbox and delivers the RCA card to
Slack, then handles the interactive feedback (thumbs up/down, resolve) and bot
commands that come back. One deployment, one Slack connection.

See the module docstring in `src/radar_feedback_service/__init__.py` for the
layout, and `docs/architecture/agent-pipeline.md` for where it sits in the
pipeline.

## Contents

- [RCA card buttons](#-rca-card-buttons)
- [`@radar` bot commands](#-radar-bot-commands)

## 🃏 RCA card buttons

Each delivered card carries three interactive buttons (`cards.py`):

| Button | Effect |
|---|---|
| 👍 Helpful | Records a `helpful` feedback row against the recommendation |
| 👎 Not helpful | Records a `not_helpful` feedback row against the recommendation |
| ✅ Resolve | Transitions the incident to `resolved` (`{open, investigating} -> resolved`) |

Resolve is dropped from the card once the incident is already resolved. Slack
Block Kit has no disabled-button state, so there is nothing to grey out; the
button is removed and a static "✅ Resolved" line takes its place. 👍/👎 stay,
since rating the RCA's usefulness remains meaningful after resolution.

## 🤖 `@radar` bot commands

Read-only: no `@radar` command mutates state (that's what the card buttons are
for). The full reference is the bot's own `_HELP` text (`bot.py`), reproduced here:

| Command | Reply |
|---|---|
| `@radar status` | Open incident count, last RCA, outbox depth |
| `@radar open` | The currently open incidents |
| `@radar incident <id>` | Details for one incident |
| `@radar last <n> [for <service>]` | The most recent incidents (`n` clamped to `RADAR_BOT_MAX_ROWS`, default 20) |
| `@radar summary [today\|yesterday]` | The day's incident summary (defaults to `today`) |
| a bare mention, `help`, or `?` | This help text |
| anything else | "Unknown command" + this help text |

**Not implemented**: `@radar close` and `@radar replay <event_id>` are both
named in the spec (`docs/implementation_plan.md`) as future work; neither
exists yet. Don't rely on either.
