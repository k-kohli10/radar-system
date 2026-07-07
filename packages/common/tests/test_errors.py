"""Error hierarchy tests."""

from __future__ import annotations

import pytest
from radar_common import (
    AuthenticationError,
    AuthorizationError,
    ConfigurationError,
    ConflictError,
    InvalidPayloadError,
    NotFoundError,
    RadarError,
    UpstreamServiceError,
)

_SUBCLASSES = [
    ConfigurationError,
    AuthenticationError,
    AuthorizationError,
    InvalidPayloadError,
    NotFoundError,
    ConflictError,
    UpstreamServiceError,
]


@pytest.mark.parametrize("error_cls", _SUBCLASSES)
def test_every_error_is_a_radar_error(error_cls: type[RadarError]) -> None:
    assert issubclass(error_cls, RadarError)
    with pytest.raises(RadarError):
        raise error_cls("boom")


def test_radar_error_is_an_exception() -> None:
    assert issubclass(RadarError, Exception)
    assert str(RadarError("msg")) == "msg"
