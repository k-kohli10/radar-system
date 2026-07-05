# ADR 0009: Slack-Only Notifications, Owned by feedback-service

## Status
Accepted

## Context
RADAR needs to deliver RCA cards to the on-call engineer and let them query incident
state conversationally. Slack is the assumed collaboration tool for the target SRE
workflow. Supporting multiple notification channels (Slack, email, PagerDuty, MS Teams)
from day one would mean building a generic notification abstraction before there's a
second real consumer to validate it against, and would split the "deliver an RCA" and
"answer a status query" features — which share the same Slack connection and the same
Postgres queries — across channel-specific code paths.

## Decision
Slack is the only notification channel. Both RCA delivery cards and the conversational
status-query bot (`@radar status`, `@radar open`, `@radar incident <id>`, etc.) are
owned by a single service, feedback-service, with a single Slack app connection
(Socket Mode locally, Events API + nginx ingress in Kubernetes). The
`NotificationBackend` protocol in `packages/contracts` exists as a plugin interface
(see [ADR 0005](0005-plugin-architecture.md)) so a future channel is *possible*, but
only one implementation — `plugins/notifications/slack/` — exists.

## Consequences
- One Slack app, one deployment, one place that holds the Slack bot/app tokens — no
  coordination between a "delivery" service and a separate "chatbot" service.
- The bot's queries hit the same Postgres tables the rest of RADAR already writes to —
  no new tables, no denormalized read model to keep in sync.
- Adding a second channel later (e.g. PagerDuty for paging, if RADAR ever needs it)
  means writing a new `NotificationBackend` plugin, not restructuring
  feedback-service — but that work is explicitly out of scope until there's a real
  need for it.
- There is no ticketing system integration (Jira, ServiceNow) — incident state lives
  entirely in Postgres and is surfaced entirely through Slack.