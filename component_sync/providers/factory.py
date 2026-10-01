"""Registry based factory for selecting a component data provider."""

from __future__ import annotations

import os

from ..exceptions import ConfigurationError
from .base import BaseProvider, ProviderRole
from .digikey import DigiKeyProvider
from .mouser import MouserProvider

__all__ = ["ProviderFactory"]


class ProviderFactory:
    """Create provider instances by name using a registry.

    The registry maps a short key such as ``"digikey"`` to a
    :class:`~component_sync.providers.base.BaseProvider` subclass. Adding a
    distributor is a one line change via :meth:`register`.

    Example:
        >>> factory = ProviderFactory()
        >>> ProviderFactory.register("acme", AcmeProvider)
        >>> provider = factory.create("acme")  # doctest: +SKIP
    """

    _registry: dict[str, type[BaseProvider]] = {}

    @classmethod
    def register(
        cls, key: str, provider_class: type[BaseProvider]
    ) -> type[BaseProvider]:
        """Register a provider implementation under ``key``.

        Args:
            key: Registry key, for example ``"digikey"``.
            provider_class: The provider class to instantiate.

        Returns:
            The registered class, so this can be used as a decorator.

        Raises:
            ConfigurationError: If ``key`` is already registered.
        """
        if key in cls._registry:
            raise ConfigurationError(f"Provider {key!r} is already registered")
        cls._registry[key] = provider_class
        return provider_class

    @classmethod
    def unregister(cls, key: str) -> None:
        """Remove a provider from the registry.

        Args:
            key: Registry key to remove.

        Raises:
            ConfigurationError: If ``key`` is not registered.
        """
        if key not in cls._registry:
            raise ConfigurationError(f"Provider {key!r} is not registered")
        del cls._registry[key]

    @classmethod
    def available(cls, role: ProviderRole | None = None) -> list[str]:
        """Return the sorted list of registered provider keys.

        Args:
            role: When given, only providers filling that role are listed. The
                two roles are not interchangeable, so the CLI offers data
                providers as the primary choice and sourcing providers
                separately.

        Returns:
            Sorted provider names.
        """
        names = sorted(cls._registry)
        if role is None:
            return names
        return [
            name
            for name in names
            if cls._registry[name].role is role
        ]

    @classmethod
    def create(
        cls,
        key: str,
        **overrides: object,
    ) -> BaseProvider:
        """Instantiate the provider registered under ``key``.

        Keyword arguments not supplied fall back to environment variables, so
        credentials can be configured either way.

        Args:
            key: Registry key of the provider to build.
            **overrides: Constructor overrides, typically credentials.

        Returns:
            A ready-to-use provider instance.

        Raises:
            ConfigurationError: If ``key`` is unknown.
        """
        provider_class = cls._registry.get(key)
        if provider_class is None:
            raise ConfigurationError(
                f"Unknown provider {key!r}. Available: {', '.join(cls.available())}"
            )
        return provider_class(**overrides)

    @classmethod
    def from_env(cls, default: str = "digikey") -> BaseProvider:
        """Build the default provider using environment credentials.

        Args:
            default: Registry key used when no override is given.

        Returns:
            A provider configured from the process environment.
        """
        overrides: dict[str, object] = {}
        if os.environ.get("DIGIKEY_CLIENT_ID"):
            overrides["client_id"] = os.environ["DIGIKEY_CLIENT_ID"]
        if os.environ.get("DIGIKEY_CLIENT_SECRET"):
            overrides["client_secret"] = os.environ["DIGIKEY_CLIENT_SECRET"]
        return cls.create(default, **overrides)


# The factory is the single extension point, so register built-ins on import.
ProviderFactory.register(DigiKeyProvider.name, DigiKeyProvider)
ProviderFactory.register(MouserProvider.name, MouserProvider)
