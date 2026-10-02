"""Sourcing provider for Mouser Electronics.

Mouser is a **sourcing** provider, not a data provider. Its Search API returns
manufacturer, description, datasheet URL and a product link, but its
``ProductAttributes`` list only ever contains ``Packaging`` and
``Standard Pack Qty``. No ``Resistance``, ``Capacitance``, ``Inductance``,
``Frequency``, ``Voltage`` or ``Operating Temperature`` was observed on any part
examined, across every search endpoint the API exposes. So this provider
contributes exactly one thing: where to buy the part.

Two behaviours of the API shape this module:

- **The key is a query parameter, not a header.** It appears as ``?apiKey=``.
- **Errors arrive as HTTP 200.** A rejected key returns ``200`` with
  ``Errors`` populated rather than a 4xx status, so checking ``status_code``
  alone would read a rejected key as success. A batch also silently drops a part
  it cannot find, reporting no error at all, so a miss is detected by
  comparing the requested part numbers against those returned.

The product URL is written exactly as returned. It carries the locale of the API
account rather than of the reader, and a ``?qs=`` tracking parameter, but
rewriting it would mean constructing a URL, and a fabricated URL is
indistinguishable from a real one in a symbol library.
"""

from __future__ import annotations

import logging
import os
import re
import time
from collections.abc import Sequence
from typing import Any

import requests

from ..exceptions import (
    AmbiguousPartError,
    ComponentSyncError,
    ConfigurationError,
    PartNotFoundError,
    ProviderAPIError,
    RateLimitError,
)
from ..models import ComponentData
from .base import BaseProvider, ProviderRole

__all__ = ["MouserProvider"]

LOGGER = logging.getLogger(__name__)

SEARCH_URL = "https://api.mouser.com/api/v1/search/partnumber"
DEFAULT_TIMEOUT = 30.0

#: Field the contributed link is written into.
#:
#: The library already uses ``Mouser`` for this, so the existing field name is
#: kept rather than introducing a second one.
LINK_FIELD = "Mouser"

#: Most part numbers Mouser accepts in one request, joined by ``|``.
#:
#: Unused for now: the processor queries one part number at a time. A future
#: batch pass would cut a 57-part run from 57 requests to 6.
MAX_PARTS_PER_REQUEST = 10

#: Minimum seconds between Mouser requests.
#:
#: Mouser allows 30 calls per minute, so one call every 2.1 seconds stays under
#: the ceiling without needing to retry.
_MIN_REQUEST_INTERVAL = 2.1

#: How many times to retry a request refused by the per-minute limit.
_MAX_RETRIES = 3

#: Upper bound on a single wait, so a malformed ``Retry-After`` cannot hang a run.
_MAX_BACKOFF = 60.0


def _is_rate_limit(status: int, payload: dict[str, Any]) -> bool:
    """Return whether a refusal is really a per-minute rate limit.

    Mouser answers a rate limit with **HTTP 403**, not 429, and identifies it in
    the body with ``Code: "TooManyRequests"`` and
    ``ResourceKey: "MaxCallPerMinute"``. Treating 403 as a permanent failure
    meant a run lost its sourcing links partway through instead of waiting.

    Args:
        status: The HTTP status code.
        payload: The decoded response body.

    Returns:
        True when the refusal is a rate limit worth waiting out.
    """
    if status == 429:
        return True
    if status != 403:
        return False
    for error in payload.get("Errors") or []:
        if not isinstance(error, dict):
            continue
        code = str(error.get("Code", ""))
        key = str(error.get("ResourceKey", ""))
        if "toomany" in code.replace(" ", "").casefold() or "maxcall" in key.casefold():
            return True
    return False


class MouserProvider(BaseProvider):
    """Contribute a Mouser purchasing link for a manufacturer part number.

    Attributes:
        name: Registry key for this provider.
        role: Always :attr:`~component_sync.providers.base.ProviderRole.SOURCE`.
        api_key: The Search API key.
        timeout: Per-request timeout in seconds.
    """

    name = "mouser"
    role = ProviderRole.SOURCE

    def __init__(
        self,
        api_key: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        """Initialise the provider.

        The key falls back to the ``MOUSER_API_KEY`` environment variable, which
        is how the sourcing pass is enabled implicitly.

        Args:
            api_key: Mouser Search API key, or ``None`` to read the environment.
            timeout: Per-request timeout in seconds.
            session: Optional pre-built session, primarily for tests.
        """
        self.api_key = api_key or os.environ.get("MOUSER_API_KEY", "")
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        #: Monotonic timestamp of the last request, for pacing.
        self._last_request: float | None = None
        #: Bulk results from :meth:`prefetch`, keyed by part number. A part
        #: present with an empty list was looked up and is not stocked.
        self._prefetched: dict[str, list[dict[str, Any]]] = {}
        #: HTTP requests actually issued, which is what the quota counts.
        self._http_requests = 0

    def authenticate(self) -> None:
        """Validate that an API key is configured.

        Mouser requires no token exchange; the key is sent with every request.

        Raises:
            ConfigurationError: If no API key is available.
        """
        if not self.api_key:
            raise ConfigurationError(
                "Mouser API key is missing. Set MOUSER_API_KEY, or pass api_key=."
            )

    def fetch_component_data(self, mpn: str) -> ComponentData:
        """Return the Mouser link for one manufacturer part number.

        Args:
            mpn: The manufacturer part number to look up.

        Returns:
            A record carrying only the Mouser link.

        Raises:
            PartNotFoundError: If Mouser does not stock the part.
            ProviderAPIError: If the request fails or the API reports an error.
        """
        self.authenticate()
        matches = self._exact_matches(mpn)
        if not matches:
            raise PartNotFoundError(mpn)
        return ComponentData(mpn=mpn, source_links={LINK_FIELD: product_url(matches[0])})

    def fetch_source_links(self, mpn: str, manufacturer: str = "") -> dict[str, str]:
        """Return the Mouser link, using the manufacturer to identify the part.

        Args:
            mpn: The manufacturer part number to look up.
            manufacturer: The manufacturer as recorded in the file. This is what
                makes a short or numeric part number resolvable, because such a
                number is unique only within a manufacturer.

        Returns:
            Mapping of field name to URL.

        Raises:
            PartNotFoundError: If Mouser does not stock the part.
            AmbiguousPartError: If the number still matches several products.
            ProviderAPIError: If the request fails or the API reports an error.
        """
        self.authenticate()
        matches = self._exact_matches(mpn)
        if not matches:
            raise PartNotFoundError(mpn)

        if len(matches) == 1:
            return {LINK_FIELD: product_url(matches[0])}

        chosen = _pick_by_manufacturer(matches, manufacturer) if manufacturer else None
        if chosen is not None:
            LOGGER.debug(
                "Disambiguated %r to %r using manufacturer %r",
                mpn,
                chosen.get("Manufacturer"),
                manufacturer,
            )
            return {LINK_FIELD: product_url(chosen)}

        raise AmbiguousPartError(
            mpn, tuple(_describe(part) for part in matches)
        )

    @property
    def http_requests(self) -> int:
        """Return how many search requests have been issued."""
        return self._http_requests

    def close(self) -> None:
        """Release any network resources held by this provider."""
        if self._owns_session:
            self._session.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    def prefetch(self, parts: Sequence[str]) -> None:
        """Resolve many part numbers in batches of ten.

        Mouser accepts up to ten pipe-separated part numbers per request, so a
        57-part run costs 6 requests instead of 57. That matters because the
        documented ceiling is only 30 calls per minute: unbatched, a 57-part run
        cannot complete inside a minute at all.

        Results are stored per part, including the empty result for a part that
        is not stocked, so a later per-part lookup never needs the network.

        Args:
            parts: The manufacturer part numbers the run will need.
        """
        wanted = [part.strip() for part in parts if part.strip()]
        unique: list[str] = []
        for part in wanted:
            if part not in unique and part not in self._prefetched:
                unique.append(part)
        for start in range(0, len(unique), MAX_PARTS_PER_REQUEST):
            chunk = unique[start : start + MAX_PARTS_PER_REQUEST]
            for part in chunk:
                self._prefetched[part] = []
            try:
                records = self._search_many(chunk)
            except ComponentSyncError as exc:
                # Leave the chunk primed as empty so the per-part path retries
                # it individually rather than losing the part.
                LOGGER.warning(
                    "Mouser prefetch of %d part(s) failed: %s", len(chunk), exc
                )
                continue
            for part in chunk:
                key = part.strip().casefold()
                for record in records:
                    if (
                        str(record.get("ManufacturerPartNumber", "")).strip().casefold()
                        == key
                    ):
                        self._prefetched[part] = [record]
                        break

    def _search_many(self, parts: list[str]) -> list[dict[str, Any]]:
        """Search for several part numbers in one request.

        Args:
            parts: Up to :data:`MAX_PARTS_PER_REQUEST` part numbers.

        Returns:
            Every product record returned, of any of the requested parts.

        Raises:
            RateLimitError: If the per-minute limit survives every retry.
            ProviderAPIError: On a transport failure, a non-200 response, a
                malformed body, or an error the API reported.
        """
        body = {"SearchByPartRequest": {"mouserPartNumber": "|".join(parts)}}
        attempt = 0
        while True:
            self._pace()
            self._http_requests += 1
            try:
                response = self._session.post(
                    f"{SEARCH_URL}?apiKey={self.api_key}",
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise ProviderAPIError(f"Mouser search failed: {exc}") from exc

            try:
                payload: dict[str, Any] = response.json()
            except ValueError as exc:
                raise ProviderAPIError(f"Malformed Mouser response: {exc}") from exc

            if _is_rate_limit(response.status_code, payload):
                attempt += 1
                if attempt > _MAX_RETRIES:
                    raise RateLimitError(
                        f"Mouser per-minute limit still refusing after "
                        f"{_MAX_RETRIES} retries (30 calls per minute)."
                    )
                wait = min(_MIN_REQUEST_INTERVAL * (2**attempt), _MAX_BACKOFF)
                LOGGER.warning(
                    "Mouser rate limit hit; waiting %.1fs (retry %d/%d)",
                    wait,
                    attempt,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            if response.status_code != 200:
                raise ProviderAPIError(f"Mouser search failed: {_summarise(response, payload)}")

            errors = payload.get("Errors") or []
            if errors:
                messages = "; ".join(
                    str(error.get("Message", "")) for error in errors if isinstance(error, dict)
                )
                raise ProviderAPIError(f"Mouser rejected the request: {messages or 'error'}")

            found: list[dict[str, Any]] = list(
                (payload.get("SearchResults") or {}).get("Parts") or []
            )
            return found

    def _pace(self) -> None:
        """Sleep if the previous request was too recent.

        Keeps a run under Mouser's 30 calls per minute without relying on the API
        to refuse work. Measuring between request starts means the pacing holds
        even when a response is slow.
        """
        if self._last_request is None:
            self._last_request = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    def _exact_matches(self, mpn: str) -> list[dict[str, Any]]:
        """Return every product whose manufacturer part number equals ``mpn``.

        More than one result is normal rather than exceptional. Manufacturer part
        numbers are unique only within a manufacturer, so a short or numeric
        number legitimately matches unrelated products from different suppliers
        of that number.

        Args:
            mpn: The manufacturer part number to look up.

        Returns:
            Every exactly matching product record.

        Raises:
            RateLimitError: If the per-minute limit survives every retry.
            ProviderAPIError: On a transport failure, a non-200 response, a
                malformed body, or an error the API reported.
        """
        # Answered from a batch prefetch, if there is one. This check comes
        # before the pacing sleep: a part already resolved must not wait 2.1
        # seconds for a request it will never make.
        cached = self._prefetched.get(mpn)
        if cached is not None:
            return list(cached)

        body = {
            "SearchByPartRequest": {
                "mouserPartNumber": mpn,
                "partSearchOptions": "Exact",
            }
        }
        attempt = 0
        while True:
            self._pace()
            try:
                response = self._session.post(
                    f"{SEARCH_URL}?apiKey={self.api_key}",
                    headers={"Content-Type": "application/json", "Accept": "application/json"},
                    json=body,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                raise ProviderAPIError(f"Mouser search failed for {mpn!r}: {exc}") from exc

            try:
                payload: dict[str, Any] = response.json()
            except ValueError as exc:
                raise ProviderAPIError(
                    f"Malformed Mouser response for {mpn!r}: {exc}"
                ) from exc

            if _is_rate_limit(response.status_code, payload):
                attempt += 1
                if attempt > _MAX_RETRIES:
                    raise RateLimitError(
                        f"Mouser per-minute limit still refusing {mpn!r} after "
                        f"{_MAX_RETRIES} retries (30 calls per minute).",
                        retry_after=int(_MIN_REQUEST_INTERVAL * attempt),
                    )
                wait = min(_MIN_REQUEST_INTERVAL * (2**attempt), _MAX_BACKOFF)
                LOGGER.warning(
                    "Mouser rate limit hit for %r; waiting %.1fs (retry %d/%d)",
                    mpn,
                    wait,
                    attempt,
                    _MAX_RETRIES,
                )
                time.sleep(wait)
                continue

            if response.status_code != 200:
                raise ProviderAPIError(
                    f"Mouser search for {mpn!r} failed: "
                    f"{_summarise(response, payload)}"
                )

            errors = payload.get("Errors") or []
            if errors:
                # A refused key arrives as HTTP 200, so the body is the only signal.
                messages = "; ".join(
                    str(error.get("Message", "")) for error in errors if isinstance(error, dict)
                )
                raise ProviderAPIError(
                    f"Mouser rejected the request for {mpn!r}: "
                    f"{messages or 'unspecified error'}"
                )

            wanted = mpn.strip().casefold()
            return [
                part
                for part in (payload.get("SearchResults") or {}).get("Parts") or []
                if str(part.get("ManufacturerPartNumber", "")).strip().casefold() == wanted
            ]

def _normalise_manufacturer(name: str) -> str:
    """Reduce a manufacturer name to comparable letters.

    Distributors do not spell a manufacturer the same way. DigiKey says
    ``"Murata Electronics"`` where Mouser says ``"Murata"``, and ``"YAGEO"`` where
    the other says ``"Yageo"``. Stripping case, spaces and punctuation lets those
    be compared without a lookup table of every vendor's naming habits.

    Args:
        name: A manufacturer name as either distributor wrote it.

    Returns:
        The name lowercased with non-alphanumeric characters removed.
    """
    return re.sub(r"[^a-z0-9]", "", name.strip().casefold())


def _same_manufacturer(left: str, right: str) -> bool:
    """Return whether two distributor spellings name the same manufacturer.

    Compares normalised forms, accepting a containment match so that a shorter
    name matches a longer one that merely elaborates it, and a shared prefix of
    at least five characters for names that differ by a corporate suffix such as
    ``"Texas Instruments"`` against ``"Texas Instruments Inc"``.

    Args:
        left: One manufacturer name.
        right: The other manufacturer name.

    Returns:
        True when the two are judged to be the same manufacturer.
    """
    a, b = _normalise_manufacturer(left), _normalise_manufacturer(right)
    if not a or not b:
        return False
    if a == b or a in b or b in a:
        return True
    shared = 0
    for first, second in zip(a, b, strict=False):
        if first != second:
            break
        shared += 1
    return shared >= 5


def _pick_by_manufacturer(
    matches: list[dict[str, Any]], manufacturer: str
) -> dict[str, Any] | None:
    """Return the single match made by ``manufacturer``, if there is exactly one.

    Args:
        matches: Every product claiming the part number.
        manufacturer: The manufacturer as recorded in the file.

    Returns:
        The matching product, or ``None`` when zero or several match.
    """
    hits = [
        part
        for part in matches
        if _same_manufacturer(_manufacturer_of(part), manufacturer)
    ]
    return hits[0] if len(hits) == 1 else None


def _manufacturer_of(part: dict[str, Any]) -> str:
    """Return the manufacturer name of a product record.

    Args:
        part: A product record from the search response.

    Returns:
        The manufacturer name, or ``""`` when the record carried none.
    """
    return str(part.get("Manufacturer") or part.get("ActualMfrName") or "").strip()


def _describe(part: dict[str, Any]) -> str:
    """Return a short ``"manufacturer: description"`` line for a candidate.

    Args:
        part: A product record from the search response.

    Returns:
        A one line description naming the manufacturer and the product.
    """
    description = " ".join(str(part.get("Description") or "").split())[:48]
    return f"{_manufacturer_of(part) or 'unknown'}: {description}"


def _summarise(response: requests.Response, payload: dict[str, Any]) -> str:
    """Return a readable one line description of a failed response.

    Args:
        response: The failed response.
        payload: The already-decoded body, if one could be read.

    Returns:
        A single line naming the status and any reason the body gave.
    """
    for key in ("message", "Message", "detail"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return f"HTTP {response.status_code}: {' '.join(value.split())}"
    for error in payload.get("Errors") or []:
        if isinstance(error, dict):
            message = str(error.get("Message", "")).strip()
            if message:
                return f"HTTP {response.status_code}: {' '.join(message.split())}"
    text = " ".join(response.text.split())[:160]
    return f"HTTP {response.status_code}: {text}" if text else f"HTTP {response.status_code}"


def product_url(part: dict[str, Any]) -> str:
    """Return the Mouser product page URL exactly as the API supplied it.

    Args:
        part: A product record from the search response.

    Returns:
        The URL, or ``""`` when the record carried none.
    """
    return str(part.get("ProductDetailUrl", "")).strip()
