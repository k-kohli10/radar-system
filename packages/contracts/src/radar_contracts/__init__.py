"""RADAR shared contracts.

Pydantic v2 domain schemas and ``typing.Protocol`` backend interfaces shared
across every RADAR service and plugin.

Hard rules for this package:

- Pydantic v2 for all models.
- ``typing.Protocol`` (never ABCs) for all backend interfaces.
- Zero vendor imports. Depends only on ``pydantic`` and the standard library.
- mypy strict must pass.

Every public contract is re-exported here, so consumers import from the top
level::

    from radar_contracts import NormalizedAlert, Incident, LLMProvider
"""

from __future__ import annotations

from .alerts import FINGERPRINT_FIELDS, NormalizedAlert, Severity
from .bot import BotCommand, BotCommandType, BotResponse
from .events import EventEnvelope, OutboxEvent, ProcessedEvent
from .feedback import FeedbackEvent
from .incidents import (
    Confidence,
    Incident,
    InvestigationPlan,
    PlanStep,
    Recommendation,
    RecommendedAction,
)
from .knowledge import EmbeddingProvider, KnowledgeStore
from .llm import (
    GatewayStreamEvent,
    LLMMode,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Message,
    Usage,
)
from .logs import LogsBackend
from .metrics import MetricsBackend
from .notifications import NotificationBackend
from .traces import Span, TracesBackend

__version__ = "0.3.0"

__all__ = [
    # alerts
    "FINGERPRINT_FIELDS",
    "NormalizedAlert",
    "Severity",
    # incidents
    "Confidence",
    "Incident",
    "InvestigationPlan",
    "PlanStep",
    "Recommendation",
    "RecommendedAction",
    # events
    "EventEnvelope",
    "OutboxEvent",
    "ProcessedEvent",
    # llm
    "GatewayStreamEvent",
    "LLMMode",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "Message",
    "Usage",
    # feedback
    "FeedbackEvent",
    # bot
    "BotCommand",
    "BotCommandType",
    "BotResponse",
    # notifications
    "NotificationBackend",
    # logs
    "LogsBackend",
    # metrics
    "MetricsBackend",
    # traces
    "Span",
    "TracesBackend",
    # knowledge
    "EmbeddingProvider",
    "KnowledgeStore",
]
