"""Abstract base class for distributor data providers."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from enum import Enum

from ..models import ComponentData

__all__ = ["BaseProvider", "ProviderRole"]


class ProviderRole(Enum):
    """What a provider is consulted for.

    Distributors are not interchangeable, and offering a single
    ``--provider`` choice between them implies they are. They are not: only some
    publish the parametric values a symbol needs.

    Attributes:
        DATA: Supplies component values: ``Value``, voltage and temperature
            bounds, package, manufacturer and description. This is the only
            kind of provider that may change those fields.
        SOURCE: Supplies purchasing links. A sourcing provider may only add a
            link the author does not already have; it can never contribute a
            value, and its absence leaves the symbol otherwise untouched.
    """

    DATA = "data"
    SOURCE = "source"


class BaseProvider(ABC):
    """Contract that every component distributor integration must satisfy.

    Subclasses translate a vendor specific API into
    :class:`~component_sync.models.ComponentData`. The rest of the package is
    written against this interface only, so adding a distributor means adding
    one module and one :func:`~component_sync.providers.factory.ProviderFactory`
    registration.

    Attributes:
        name: Registry key for this provider, for example ``"digikey"``.
        role: What this provider is consulted for.
    """

    name: str = "base"
    role: ProviderRole = ProviderRole.DATA

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

    def fetch_source_links(
        self, mpn: str, manufacturer: str = ""
    ) -> dict[str, str]:
        """Return purchasing links for a part, optionally disambiguated.

        A separate hook from :meth:`fetch_component_data` because a sourcing
        provider may need more context than a bare part number to identify a
        product. Manufacturer part numbers are unique only within a
        manufacturer, and distributors index them the same way, so a short or
        purely numeric number can match unrelated products. A provider that can
        use the manufacturer to pick the right one overrides this.

        The default implementation ignores the hint and returns whatever links
        :meth:`fetch_component_data` produced, which is correct for any provider
        whose part numbers are unambiguous.

        Args:
            mpn: The manufacturer part number to look up.
            manufacturer: The manufacturer as recorded in the file, used to
                disambiguate. May be empty.

        Returns:
            Mapping of field name to URL.

        Raises:
            PartNotFoundError: If the provider has no such part.
            AmbiguousPartError: If the number matches several products and the
                manufacturer does not single one out.
        """
        _ = manufacturer
        return self.fetch_component_data(mpn).source_properties()

    @property
    def http_requests(self) -> int:
        """How many HTTP requests this provider has actually issued.

        Quotas are counted in HTTP requests, not in method calls. Once a
        provider prefetches a run in batches, its per-part lookups are answered
        from memory, so counting those would report a far larger figure than the
        distributor ever saw and would make quota use impossible to judge.
        """
        return 0

    def prefetch(self, parts: Sequence[str]) -> None:  # noqa: B027 - optional hook
        """Resolve several part numbers in as few requests as the API allows.

        A provider whose endpoint accepts more than one part number per request
        can answer a whole run far more cheaply than one call per part: Mouser
        takes ten, which turns a 57-part run from 57 requests into 6. Providers
        that only handle one part number per call do nothing here.

        This is an optimisation, never a requirement. A part that was not
        prefetched, or whose prefetch failed, is still resolved individually by
        :meth:`fetch_component_data`, so correctness does not depend on it.

        Args:
            parts: The manufacturer part numbers the run will need.
        """

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
