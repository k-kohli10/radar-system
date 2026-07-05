"""Plugin registry with protocol-conformance checking.

``PluginRegistry`` maps a ``(contract Protocol, name)`` pair to an implementing
class. Registration verifies conformance two ways:

- **Statically**, via the ``type[T]`` signatures: mypy rejects an ``impl`` that
  is not a structural subtype of the ``protocol``.
- **At runtime**, via ``issubclass(impl, protocol)``. Every ``radar-contracts``
  backend interface is a method-only ``@runtime_checkable`` Protocol, so this
  structural check is valid.

The registry itself imports no vendor SDK and no concrete plugin.
"""

from __future__ import annotations

from typing import Any, TypeVar, cast

from .base import PluginConformanceError, PluginMetadata, PluginNotFoundError

T = TypeVar("T")


def _conforms(impl: type[Any], protocol: type[Any]) -> bool:
    """Return whether ``impl`` structurally satisfies ``protocol``.

    ``protocol`` must be a method-only ``@runtime_checkable`` Protocol; otherwise
    ``issubclass`` is not permitted and a ``PluginConformanceError`` is raised.
    """
    try:
        return issubclass(impl, protocol)
    except TypeError as exc:
        raise PluginConformanceError(
            f"{getattr(protocol, '__name__', protocol)!r} must be a method-only "
            "@runtime_checkable Protocol to check conformance"
        ) from exc


class PluginRegistry:
    """A registry of backend plugins keyed by contract Protocol and name."""

    def __init__(self) -> None:
        self._impls: dict[tuple[type[Any], str], type[Any]] = {}
        self._metadata: dict[tuple[type[Any], str], PluginMetadata] = {}

    def register(
        self,
        protocol: type[T],
        impl: type[T],
        *,
        name: str,
        version: str = "0.1.0",
    ) -> None:
        """Register ``impl`` under ``protocol`` and ``name``.

        Raises ``PluginConformanceError`` if ``impl`` does not implement
        ``protocol``, or if ``name`` is already registered for ``protocol``.
        """
        if not _conforms(impl, protocol):
            raise PluginConformanceError(
                f"{impl.__name__!r} does not implement "
                f"{getattr(protocol, '__name__', protocol)!r}"
            )
        key = (protocol, name)
        if key in self._impls:
            raise PluginConformanceError(
                f"a plugin named {name!r} is already registered for "
                f"{getattr(protocol, '__name__', protocol)!r}"
            )
        self._impls[key] = impl
        self._metadata[key] = PluginMetadata(name=name, version=version)

    def get(self, protocol: type[T], name: str) -> type[T]:
        """Return the implementation class registered for ``protocol``/``name``."""
        try:
            impl = self._impls[(protocol, name)]
        except KeyError:
            raise PluginNotFoundError(
                f"no plugin named {name!r} registered for "
                f"{getattr(protocol, '__name__', protocol)!r}"
            ) from None
        return cast("type[T]", impl)

    def create(self, protocol: type[T], name: str, /, *args: Any, **kwargs: Any) -> T:
        """Instantiate the plugin registered for ``protocol``/``name``."""
        impl = self.get(protocol, name)
        return impl(*args, **kwargs)

    def names(self, protocol: type[Any]) -> list[str]:
        """Return the sorted names registered for ``protocol``."""
        return sorted(n for (p, n) in self._impls if p is protocol)

    def metadata(self, protocol: type[Any], name: str) -> PluginMetadata:
        """Return the metadata for the plugin at ``protocol``/``name``."""
        try:
            return self._metadata[(protocol, name)]
        except KeyError:
            raise PluginNotFoundError(
                f"no plugin named {name!r} registered for "
                f"{getattr(protocol, '__name__', protocol)!r}"
            ) from None
