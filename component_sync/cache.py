"""Session-level caching of provider lookups.

A bill of materials names the same part many times over: 166 component instances
in this project resolve to only 56 distinct manufacturer part numbers. Without
caching, every instance becomes an API request, which is both slow and a
straight path to a distributor rate limit.

The cache is keyed on the provider identity plus the normalised MPN, so two
providers in the same session never see each other's data. Both hits *and*
misses are remembered: a part that is not stocked is just as repeated as one that
is, and re-querying it wastes the same request.

Only successful lookups and definitive misses are cached. A transport error or a
server-side failure is *not*, because the next run may well succeed and caching a
transient fault would silently drop a part that is actually available.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .exceptions import PartNotFoundError
from .models import ComponentData
from .providers.base import BaseProvider

__all__ = ["LookupCache", "CacheStats", "normalise_mpn"]


def normalise_mpn(mpn: str) -> str:
    """Return the comparison key for a manufacturer part number.

    Surrounding whitespace is ignored and case is folded, because distributors
    and hand-entered BOMs disagree on both. Nothing else is changed: separators
    inside a part number such as NXP's ``PCA85162T/Q900/1Y`` are significant and
    stripping them would produce a part that does not exist.

    Args:
        mpn: The manufacturer part number as stored in the file.

    Returns:
        The normalised key.
    """
    return mpn.strip().casefold()


@dataclass(frozen=True)
class CacheStats:
    """Counters describing cache effectiveness for one session.

    Attributes:
        hits: Requests served without contacting the provider.
        misses: Requests that reached the provider and succeeded.
        negative_hits: Requests served from a remembered *miss*.
        failures: Requests that reached the provider and failed, and so were not
            cached.
    """

    hits: int = 0
    misses: int = 0
    negative_hits: int = 0
    failures: int = 0

    @property
    def requests_made(self) -> int:
        """Return how many network requests were actually issued."""
        return self.misses + self.failures

    @property
    def saved(self) -> int:
        """Return how many requests the cache avoided."""
        return self.hits + self.negative_hits

    def describe(self) -> str:
        """Return a one line summary for the run report.

        Returns:
            A human readable summary of cache effectiveness.
        """
        return (
            f"cache: {self.hits + self.negative_hits} reused, "
            f"{self.requests_made} requested "
            f"(+{self.hits} value hits, +{self.negative_hits} miss hits, "
            f"{self.failures} failed)"
        )


@dataclass
class LookupCache:
    """Memoises provider lookups for the lifetime of a run.

    Attributes:
        _values: Resolved data keyed by ``(provider_key, mpn)``.
        _missing: Remembered misses keyed the same way.
        _stats: Effectiveness counters.
    """

    _values: dict[tuple[BaseProvider, str], ComponentData] = field(default_factory=dict)
    _missing: set[tuple[BaseProvider, str]] = field(default_factory=set)
    _stats: CacheStats = field(default_factory=CacheStats)

    @staticmethod
    def _provider_key(provider: BaseProvider) -> BaseProvider:
        """Return the identity a provider is cached under.

        The provider object itself is the key, so it is compared by identity and
        two instances never share entries even when they are the same class
        pointed at different accounts. The registry ``name`` would be wrong:
        every DigiKey provider is called ``digikey``.

        Args:
            provider: The provider being queried.

        Returns:
            The provider, used as a dictionary key.
        """
        return provider

    def fetch(self, provider: BaseProvider, mpn: str) -> ComponentData | None:
        """Return data for ``mpn``, consulting the provider at most once.

        Args:
            provider: Provider to query on a cache miss.
            mpn: The manufacturer part number.

        Returns:
            The component data, or ``None`` when the provider has no such part.

        Raises:
            ComponentSyncError: Any provider failure other than a missing part.
                These are never cached, so a transient fault is retried.
        """
        key = (self._provider_key(provider), normalise_mpn(mpn))

        if key in self._values:
            self._bump(hits=1)
            return self._values[key]
        if key in self._missing:
            self._bump(negative_hits=1)
            return None

        try:
            data = provider.fetch_component_data(mpn)
        except PartNotFoundError:
            self._missing.add(key)
            self._bump(misses=1)
            return None
        except BaseException:
            # Transport and server faults are transient by nature. Not caching
            # them means the next occurrence gets a genuine retry instead of a
            # permanently poisoned entry.
            self._bump(failures=1)
            raise

        self._values[key] = data
        self._bump(misses=1)
        return data

    def _bump(
        self,
        *,
        hits: int = 0,
        misses: int = 0,
        negative_hits: int = 0,
        failures: int = 0,
    ) -> None:
        """Increment one or more counters.

        Args:
            hits: Value-cache hits to add.
            misses: Provider requests to add.
            negative_hits: Remembered-miss hits to add.
            failures: Failed provider requests to add.
        """
        current = self._stats
        self._stats = CacheStats(
            hits=current.hits + hits,
            misses=current.misses + misses,
            negative_hits=current.negative_hits + negative_hits,
            failures=current.failures + failures,
        )

    @property
    def stats(self) -> CacheStats:
        """Return the current effectiveness counters."""
        return self._stats

    def clear(self) -> None:
        """Discard all cached entries and reset the counters."""
        self._values.clear()
        self._missing.clear()
        self._stats = CacheStats()



