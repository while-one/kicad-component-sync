"""Custom exception hierarchy for :mod:`component_sync`.

All errors raised deliberately by this package derive from
:class:`ComponentSyncError`, which makes it possible for callers (the CLI, the
KiCad plugin, or library users) to catch every expected failure with a single
``except`` clause while still distinguishing between failure modes.
"""

from __future__ import annotations

__all__ = [
    "ComponentSyncError",
    "ProviderAPIError",
    "RateLimitError",
    "PartNotFoundError",
    "AmbiguousPartError",
    "ConfigurationError",
    "FileFormatError",
]


class ComponentSyncError(Exception):
    """Base class for every error raised by :mod:`component_sync`.

    Attributes:
        message: Human readable description of the failure.
    """

    def __init__(self, message: str) -> None:
        """Initialise the error.

        Args:
            message: Human readable description of the failure.
        """
        super().__init__(message)
        self.message = message


class ProviderAPIError(ComponentSyncError):
    """Raised when a distributor API is unreachable or returns an error.

    This covers transport failures, non-2xx HTTP responses, malformed
    payloads and authentication failures.
    """


class RateLimitError(ProviderAPIError):
    """Raised when a distributor refuses work because a quota is exhausted.

    Distinguished from a generic :class:`ProviderAPIError` because the two
    demand opposite responses. A per-minute burst is worth waiting out, so the
    provider retries it automatically. A daily quota is not: waiting three hours
    mid-run helps nobody, so the run stops immediately and says when the quota
    returns. Callers can also use this to tell the author their request was
    refused rather than that their data is wrong.

    Attributes:
        retry_after: Seconds the distributor asked the client to wait.
        resets_at: Human readable time the quota window resets, when advertised.
        daily: True when the daily quota was exhausted rather than a burst.
    """

    def __init__(
        self,
        message: str,
        *,
        retry_after: int = 0,
        resets_at: str = "",
        daily: bool = False,
    ) -> None:
        """Initialise the error.

        Args:
            message: Human readable description of the refusal.
            retry_after: Seconds to wait before retrying.
            resets_at: When the quota window resets, as advertised.
            daily: True when this is the daily quota rather than a burst.
        """
        super().__init__(message)
        self.retry_after = retry_after
        self.resets_at = resets_at
        self.daily = daily


class PartNotFoundError(ComponentSyncError):
    """Raised when a manufacturer part number has no match at the provider.

    Attributes:
        mpn: The manufacturer part number that could not be resolved.
    """

    def __init__(self, mpn: str, message: str | None = None) -> None:
        """Initialise the error.

        Args:
            mpn: The manufacturer part number that could not be resolved.
            message: Optional override for the default message.
        """
        super().__init__(message or f"Part not found: {mpn!r}")
        self.mpn = mpn


class AmbiguousPartError(ComponentSyncError):
    """Raised when a part number matches several unrelated products.

    Manufacturer part numbers are only unique within a manufacturer, so a bare
    number is ambiguous across a catalogue. Mouser, for instance, returns seven
    different products all numbered ``1028``: side cutting pliers, conduit
    fittings, an eInk display, a battery holder, punches, a printer shaft and a
    tape measure. Picking the first result would attach a link to pliers to a
    battery holder symbol, so the ambiguity is reported instead.

    Attributes:
        mpn: The ambiguous manufacturer part number.
        candidates: Human readable ``"manufacturer: description"`` lines, one
            per product that claimed the number.
    """

    def __init__(self, mpn: str, candidates: tuple[str, ...]) -> None:
        """Initialise the error.

        Args:
            mpn: The ambiguous manufacturer part number.
            candidates: Descriptions of the competing products.
        """
        listed = ", ".join(candidates)
        super().__init__(
            f"Part number {mpn!r} matches {len(candidates)} unrelated products "
            f"({listed}). It needs a manufacturer to identify one."
        )
        self.mpn = mpn
        self.candidates = candidates


class ConfigurationError(ComponentSyncError):
    """Raised when required credentials or options are missing."""


class FileFormatError(ComponentSyncError):
    """Raised when an input file cannot be parsed as its declared format."""
