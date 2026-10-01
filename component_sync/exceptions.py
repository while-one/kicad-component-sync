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
    "PartNotFoundError",
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


class ConfigurationError(ComponentSyncError):
    """Raised when required credentials or options are missing."""


class FileFormatError(ComponentSyncError):
    """Raised when an input file cannot be parsed as its declared format."""
