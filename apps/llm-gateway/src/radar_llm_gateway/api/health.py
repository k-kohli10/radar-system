"""Liveness and readiness probes.

``/healthz`` answers 200 whenever the process is alive, nothing more.

``/readyz`` makes the stronger claim that this gateway can serve LLM traffic.
Startup (``main.py``) loads the mode config, the token map from Vault, and
builds every provider binding, reading each referenced vendor's API key from
the Vault secret files. Only if all of that succeeded does startup mark
:class:`Readiness` ready; otherwise ``/readyz`` answers 503 with the
(secret-free) reason.

Neither probe requires an agent token (they sit outside ``/v1/``), and neither
logs.
"""

from __future__ import annotations

from fastapi import APIRouter, Response, status


class Readiness:
    """Mutable readiness state, set by startup and read by ``/readyz``.

    Starts not-ready ("starting"); :meth:`mark_ready` flips it after the config,
    token map, and every provider binding (API keys included) have loaded.
    ``reason`` strings must be safe to return to a probe: the config layer's
    ``ConfigurationError``/``SecretNotFoundError`` messages name files and
    modes, never secret values.
    """

    def __init__(self) -> None:
        self._reason: str | None = "starting"

    def mark_ready(self) -> None:
        self._reason = None

    def mark_not_ready(self, reason: str) -> None:
        self._reason = reason or "not ready"

    @property
    def reason(self) -> str | None:
        """Why the gateway is not ready, or None when it is."""
        return self._reason


def create_health_router(readiness: Readiness) -> APIRouter:
    """Build the ``/healthz`` and ``/readyz`` routes over ``readiness``."""
    router = APIRouter()

    @router.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/readyz")
    async def readyz(response: Response) -> dict[str, str]:
        reason = readiness.reason
        if reason is not None:
            response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
            return {"status": "not_ready", "reason": reason}
        return {"status": "ready"}

    return router
