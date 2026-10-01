"""Abstract base class for distributor data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..models import ComponentData

__all__ = ["BaseProvider"]


class BaseProvider(ABC):
    """Contract that every component distributor integration must satisfy.

    Subclasses translate a vendor specific API into
    :class:`~component_sync.models.ComponentData`. The rest of the package is
    written against this interface only, so adding a distributor means adding
    one module and one :func:`~component_sync.providers.factory.ProviderFactory`
    registration.

    Attributes:
        name: Registry key for this provider, for example ``"digikey"``.
    """

    name: str = "base"

    @abstractmethod
    def authenticate(self) -> None:
        """Obtain or refresh credentials with the distributor API.

        Implementations must be idempotent so callers can authenticate before
        every batch of lookups.

        Raises:
            ProviderAPIError: If credentials are missing, rejected, or the
                endpoint cannot be reached.
        """
        raise NotImplementedError

    @abstractmethod
    def fetch_component_data(self, mpn: str) -> ComponentData:
        """Retrieve normalised parametric data for one manufacturer part number.

        Args:
            mpn: The manufacturer part number to look up.

        Returns:
            The normalised component record.

        Raises:
            PartNotFoundError: If the distributor has no such part.
            ProviderAPIError: If the lookup fails for any other reason.
        """
        raise NotImplementedError

    def close(self) -> None:  # noqa: B027 - optional hook, deliberately concrete
        """Release any network resources held by this provider.

        This is an optional hook rather than an abstract method: providers that
        hold no resources should not be forced to override it. The default
        implementation is therefore intentionally a no-op.
        """

    def __enter__(self) -> BaseProvider:
        """Enter a context manager that closes the provider on exit.

        Returns:
            This provider instance.
        """
        return self

    def __exit__(self, *exc_info: object) -> None:
        """Close the provider when leaving the context manager.

        Args:
            *exc_info: Standard exception triple, unused.
        """
        self.close()
