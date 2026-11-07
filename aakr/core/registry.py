"""Provider registry and selection.

Business logic must not depend on provider-specific imports.
Provider initialization must validate required capabilities and reject
unsupported operations.
"""

from __future__ import annotations

from typing import Any, Protocol

from aakr.core.store import CatalogStore


class ProviderFactory(Protocol):
    def __call__(self, **config: Any) -> CatalogStore: ...


class ProviderRegistry:
    """Registry of storage providers. Selects the configured backend."""

    def __init__(self):
        self._providers: dict[str, ProviderFactory] = {}

    def register(self, name: str, factory: ProviderFactory) -> None:
        if name in self._providers:
            raise ValueError(f"provider {name!r} is already registered")
        self._providers[name] = factory

    def get_store(self, provider: str, **config: Any) -> CatalogStore:
        if provider not in self._providers:
            raise ValueError(
                f"unknown provider {provider!r}; available: {sorted(self._providers)}"
            )
        return self._providers[provider](**config)

    def available_providers(self) -> list[str]:
        return sorted(self._providers)


# Default registry instance
_default = ProviderRegistry()


def register_provider(name: str, factory: ProviderFactory) -> None:
    _default.register(name, factory)


def get_store(provider: str, **config: Any) -> CatalogStore:
    return _default.get_store(provider, **config)


def available_providers() -> list[str]:
    return _default.available_providers()


def register_builtin_providers():
    """Register built-in providers. Call from the integrations layer,
    not from core. Core must not import provider-specific modules.
    """
    raise NotImplementedError(
        "Call aakr.integrations.register_builtin_providers() instead"
    )
