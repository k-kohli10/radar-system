"""Standard service startup wiring.

Every RADAR service repeats the same first steps: load non-secret settings from
the environment, configure JSON logging, and (for services with guarded
endpoints) load its ``agent_token`` from Vault and build the auth dependency.
:func:`bootstrap` collapses that into one call and returns a
:class:`ServiceRuntime` bundle the service's ``main.py`` wires into FastAPI::

    rt = bootstrap(Settings)

    app = FastAPI()

    @app.post("/events", dependencies=[Depends(rt.auth)])
    async def receive_event(...): ...

The llm-gateway is the exception: it has no inbound ``agent_token`` of its own
(it validates *callers'* tokens against a token→mode map), so it calls
``bootstrap(Settings, with_agent_auth=False)`` and builds its own auth.
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import SecretStr
from structlog.typing import FilteringBoundLogger

from .auth import AgentTokenAuth
from .config import RadarSettings, read_secret
from .logging import configure_logging, get_logger

AGENT_TOKEN_SECRET = "agent_token"
"""Vault secret filename holding a service's own agent token."""

AGENT_TOKEN_PREVIOUS_SECRET = "agent_token_previous"
"""Vault secret filename holding the immediately-prior agent token.

Written only during a rotation (by ``make rotate``) and cleared once the roll has
converged (by ``make rotate-finalize``). A service accepts both this and the current
token so a hot rotation never rejects the still-in-flight one — closing the
transient-401 window that used to dead-letter events mid-roll.
"""


def load_agent_tokens() -> list[SecretStr]:
    """Return the agent token(s) a service accepts: current, plus previous if present.

    The current ``agent_token`` is required — a missing file raises
    :class:`~radar_common.config.SecretNotFoundError`, failing startup loudly, exactly
    as before. ``agent_token_previous`` is optional, so in steady state this is a
    single-element list; during a rotation it holds both. Both are accepted by
    :class:`~radar_common.auth.AgentTokenAuth`.
    """
    current = read_secret(AGENT_TOKEN_SECRET)
    assert current is not None  # required=True: read_secret raised if absent
    previous = read_secret(AGENT_TOKEN_PREVIOUS_SECRET, required=False)
    return [token for token in (current, previous) if token is not None]


@dataclass(frozen=True)
class ServiceRuntime[S: RadarSettings]:
    """The result of :func:`bootstrap`: everything a service needs at startup.

    ``auth`` is ``None`` only when ``bootstrap`` was called with
    ``with_agent_auth=False`` (the llm-gateway); otherwise it is a ready
    :class:`~radar_common.auth.AgentTokenAuth` guarding the service's endpoints.
    """

    settings: S
    log: FilteringBoundLogger
    auth: AgentTokenAuth | None


def bootstrap[S: RadarSettings](
    settings_cls: type[S], *, with_agent_auth: bool = True
) -> ServiceRuntime[S]:
    """Perform standard service startup and return a :class:`ServiceRuntime`.

    Loads ``settings_cls`` from the environment, configures JSON logging for the
    service, and — unless ``with_agent_auth`` is False — loads the service's
    ``agent_token`` from Vault and builds an :class:`AgentTokenAuth`. A missing
    token file raises :class:`~radar_common.config.SecretNotFoundError`, failing
    startup loudly (the Vault init-container writes the file before this
    container runs).
    """
    settings = settings_cls()
    configure_logging(service_name=settings.service_name, level=settings.log_level)
    log = get_logger(settings.service_name)

    auth: AgentTokenAuth | None = None
    if with_agent_auth:
        auth = AgentTokenAuth(load_agent_tokens())

    log.info(
        "service.bootstrapped",
        environment=settings.environment,
        agent_auth=with_agent_auth,
    )
    return ServiceRuntime(settings=settings, log=log, auth=auth)
