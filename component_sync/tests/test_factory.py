"""Tests for the provider registry and factory."""

from __future__ import annotations

import pytest

from component_sync.exceptions import ConfigurationError
from component_sync.models import ComponentData
from component_sync.providers.base import BaseProvider
from component_sync.providers.digikey import DigiKeyProvider
from component_sync.providers.factory import ProviderFactory


class AlphaProvider(BaseProvider):
    """Extra provider used to exercise registration.

    Unlike :class:`StubProvider` it takes no constructor arguments, so it also
    proves that the factory can build a provider with an empty override set.
    """

    name = "alpha"

    def authenticate(self) -> None:
        """No-op."""

    def fetch_component_data(self, mpn: str) -> ComponentData:
        """Return an empty record for ``mpn``.

        Args:
            mpn: Part number, ignored.

        Returns:
            An empty component record.
        """
        return ComponentData(mpn=mpn)


class TestProviderFactory:
    """Verify registry behaviour."""

    def test_builtin_provider_is_registered(self) -> None:
        """DigiKey is available without explicit registration."""
        assert "digikey" in ProviderFactory.available()
        assert ProviderFactory.available() == sorted(ProviderFactory.available())

    def test_create_returns_requested_class(self) -> None:
        """Creating by key yields an instance of the registered class."""
        provider = ProviderFactory.create("digikey", client_id="a", client_secret="b")
        assert isinstance(provider, DigiKeyProvider)
        assert provider.name == "digikey"

    def test_create_passes_overrides_through(self) -> None:
        """Keyword overrides reach the provider constructor."""
        provider = ProviderFactory.create("digikey", client_id="id", client_secret="sec")
        assert isinstance(provider, DigiKeyProvider)
        assert provider.client_id == "id"
        assert provider.client_secret == "sec"

    def test_unknown_provider_raises(self) -> None:
        """An unregistered key is a configuration error."""
        with pytest.raises(ConfigurationError, match="Unknown provider"):
            ProviderFactory.create("does-not-exist")

    def test_register_and_unregister_roundtrip(self) -> None:
        """A provider can be added and removed."""
        try:
            ProviderFactory.register("alpha", AlphaProvider)
            assert "alpha" in ProviderFactory.available()
            assert isinstance(ProviderFactory.create("alpha"), AlphaProvider)
        finally:
            ProviderFactory.unregister("alpha")
        assert "alpha" not in ProviderFactory.available()

    def test_duplicate_registration_rejected(self) -> None:
        """Registering the same key twice is refused."""
        with pytest.raises(ConfigurationError, match="already registered"):
            ProviderFactory.register("digikey", AlphaProvider)

    def test_unregister_unknown_raises(self) -> None:
        """Removing an unregistered key is refused."""
        with pytest.raises(ConfigurationError, match="not registered"):
            ProviderFactory.unregister("nope")

    def test_from_env_uses_environment(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Credentials are read from the environment when not overridden."""
        monkeypatch.setenv("DIGIKEY_CLIENT_ID", "env-id")
        monkeypatch.setenv("DIGIKEY_CLIENT_SECRET", "env-secret")
        provider = ProviderFactory.from_env()
        assert isinstance(provider, DigiKeyProvider)
        assert provider.client_id == "env-id"
        assert provider.client_secret == "env-secret"

    def test_from_env_without_credentials(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent credentials leave the provider unconfigured."""
        monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
        monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
        provider = ProviderFactory.from_env()
        assert isinstance(provider, DigiKeyProvider)
        assert provider.client_id == ""
        assert provider.client_secret == ""

    def test_registry_is_shared_with_subclass(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The built-in registration is visible to the class, not one instance."""
        assert isinstance(ProviderFactory._registry["digikey"], type)
        assert issubclass(ProviderFactory._registry["digikey"], BaseProvider)
        _ = monkeypatch
