# 💬 ADR 0009: Slack-Only Notifications, Owned by feedback-service

## Contents

- [Status](#-status)
- [Context](#-context)
- [Decision](#-decision)
- [Consequences](#-consequences)
- [Comparison](#-comparison)

## 🚦 Status
Accepted

## 🧩 Context
RADAR needs to deliver RCA cards to the on-call engineer and let them query incident
state conversationally. Slack is the assumed collaboration tool for the target SRE
workflow.

## ✅ Decision
Slack is the only notification channel. Both RCA delivery cards and the conversational
status-query bot (`@radar status`, `@radar open`, `@radar incident <id>`, etc.) are
owned by a single service, feedback-service, with a single Slack app connection
(Socket Mode locally, Events API + nginx ingress in Kubernetes). The
`NotificationBackend` protocol in `packages/contracts` exists as a plugin interface
(see [ADR 0005](0005-plugin-architecture.md)) so a future channel is possible, but
only one implementation, `plugins/notifications/slack/`, exists today.

## ⚖️ Consequences
- One Slack app, one deployment, one place that holds the Slack bot/app tokens. No
  coordination between a "delivery" service and a separate "chatbot" service.
- The bot's queries hit the same Postgres tables the rest of RADAR already writes to.
  No new tables, no denormalized read model to keep in sync.
- Adding a second channel later (e.g. PagerDuty for paging, if RADAR ever needs it)
  means writing a new `NotificationBackend` plugin, not restructuring feedback-service.
  That work is explicitly out of scope until there's a real need for it.
- Incident state lives entirely in Postgres and is surfaced entirely through Slack.

## 🆚 Comparison

| Alternative | What it's for | Why RADAR skips it |
|---|---|---|
| Generic multi-channel notification abstraction (Slack, email, PagerDuty, MS Teams) from day one | Supporting several notification channels up front | No second real consumer exists yet to validate the abstraction against; it would split RCA delivery and status-query logic, which share one Slack connection and one set of Postgres queries, across channel-specific code paths |
