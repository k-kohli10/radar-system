# 📜 radar-contracts

Shared contracts for RADAR: Pydantic v2 domain schemas and backend `Protocol`
interfaces used across every service and plugin.

## Rules

- **Pydantic v2** for all models.
- **`typing.Protocol`** for all backend interfaces.
- **Zero vendor imports.** No `anthropic`, `openai`, `google-generativeai`,
  `slack-sdk`, Elasticsearch, or any other SDK. This package depends only on
  `pydantic` and the standard library.
- **mypy strict** must pass.

## Contents

| Module           | Defines                                                        |
| ---------------- | ------------------------------------------------------------- |
| `alerts.py`      | `NormalizedAlert`                                             |
| `incidents.py`   | `Incident`, `InvestigationPlan`, `Recommendation`            |
| `events.py`      | `OutboxEvent`, `ProcessedEvent`                              |
| `llm.py`         | `LLMRequest`, `LLMResponse`, `GatewayStreamEvent`, `LLMProvider` |
| `feedback.py`    | `FeedbackEvent`                                               |
| `notifications.py` | `NotificationBackend`                                       |
| `bot.py`         | `BotCommand`, `BotResponse`                                   |
| `logs.py`        | `LogsBackend`                                                 |
| `metrics.py`     | `MetricsBackend`                                              |
| `traces.py`      | `TracesBackend`                                               |
| `knowledge.py`   | `EmbeddingProvider`, `KnowledgeStore`                        |

Consumers import from the top-level package, e.g.:

```python
from radar_contracts import NormalizedAlert, Incident, LLMProvider
```
