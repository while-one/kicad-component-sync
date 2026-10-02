"""DigiKey Electronics provider implementation.

DigiKey exposes an OAuth2 protected product information API. This module
handles the token exchange and maps the response payload onto
:class:`~component_sync.models.ComponentData`.

Reference:
    https://developer.digikey.com/documentation
"""

from __future__ import annotations

import base64
import logging
import os
import re
import time
from typing import Any

import requests

from ..exceptions import (
    ConfigurationError,
    PartNotFoundError,
    ProviderAPIError,
    RateLimitError,
)
from ..models import ComponentData
from ..ranges import parse_range
from ..values import derive_value
from .base import BaseProvider

__all__ = ["DigiKeyProvider"]

LOGGER = logging.getLogger(__name__)

#: Characters treated as separators when comparing vendor parameter names.
_SEPARATORS = re.compile(r"[\s/_-]+")

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
#: Product search endpoint.
#:
#: This is a POST with a JSON body, not a GET carrying the part number in the
#: path. The ``/products/v4/search/keyword`` shape was confirmed against the
#: published OpenAPI document; the earlier guess of
#: ``/products/v4/search/{keyword}`` answers 404 for every request, which made
#: every single part look like a miss.
PRODUCT_URL = "https://api.digikey.com/products/v4/search/keyword"
DEFAULT_TIMEOUT = 30.0

#: Parameter names normalised onto dedicated :class:`ComponentData` attributes.
_TEMP_KEYS = ("operating_temperature", "temperature_range", "operating_temp")
_VOLTAGE_KEYS = ("voltage_supply", "voltage_rated", "voltage_rating", "supply_voltage")

#: Parameters consulted for the package designation, in priority order.
#:
#: Package is not a top-level field in this API. ``Package / Case`` is the
#: distributor's own wording and is preferred; ``Supplier Device Package`` is
#: the JEDEC-style designation some manufacturers use instead.
_PACKAGE_KEYS = ("package_case", "supplier_device_package", "package")

#: Categories whose ``Value`` is a quantity rather than a part number.
#:
#: Deriving a Value is only safe for discrete passives, where the label really is
#: the component's defining characteristic. Integrated circuits, modules,
#: switches and development boards conventionally carry their part number as
#: the Value, and overwriting it would destroy them.
#:
#: The NFC reader ``PN7160A1HN/C100E`` is the case that matters: it publishes
#: ``Frequency = 13.56MHz``, so a parameter-only rule would replace its part
#: number with "13.56 MHz". The category is the distributor's own taxonomy, so
#: this is vendor data rather than a hand-maintained list.
_PASSIVE_CATEGORIES = frozenset(
    {
        "capacitors",
        "resistors",
        "inductors, coils, chokes",
        "crystals, oscillators, resonators",
    }
)

#: Minimum seconds between product requests.
#:
#: Product Information is limited to 120 requests per minute and 1000 per day. A
#: full run over this library spends roughly 70 of the daily allowance, so the
#: daily quota is the binding constraint rather than the burst. Pacing at about
#: 109 requests per minute keeps a run clear of the per-minute ceiling without
#: adding noticeable time: 57 parts take roughly half a minute.
_MIN_REQUEST_INTERVAL = 0.55

#: How many times to retry a request refused by the per-minute burst limit.
#:
#: A burst clears within seconds, so retrying is worthwhile. The daily quota is
#: not retried at all, because it can be hours away.
_MAX_BURST_RETRIES = 3

#: How long to wait before the first burst retry, when no header says otherwise.
_INITIAL_BACKOFF = 2.0

#: Upper bound on a single wait, so a malformed ``Retry-After`` cannot hang a run.
_MAX_BACKOFF = 60.0
#:
#: 50 is the largest value the API accepts; anything higher is rejected with
#: HTTP 400. This matters more than it looks. The search is a fuzzy, tokenised
#: match, so a short numeric part number can return tens of thousands of
#: candidates: querying ``1028`` returns 19,963 products, and the Keystone part
#: of that number sits around position 50. With a limit of 10 it fell outside the
#: window and the part was reported as missing when it is in fact stocked.
_SEARCH_LIMIT = 50

#: How many pages to walk when the first page holds no exact match.
#:
#: Paging is only reached when a part genuinely is not on page one, so it costs
#: nothing for the common case of a distinctive part number that matches
#: immediately. The cap keeps a query such as a bare ``1028`` from walking all
#: 19,963 results, and a part not found within the window is honestly reported
#: as missing rather than guessed at.
_MAX_SEARCH_PAGES = 4


class DigiKeyProvider(BaseProvider):
    """Fetch component data from the DigiKey product information API.

    The provider keeps a single :class:`requests.Session` so that TCP and TLS
    connections are reused across lookups, which matters when enriching a BOM
    with hundreds of line items. The session is closed by :meth:`close`.

    Attributes:
        name: Registry key for this provider.
        client_id: OAuth2 client identifier.
        client_secret: OAuth2 client secret.
        timeout: Per-request timeout in seconds.
    """

    name = "digikey"

    def __init__(
        self,
        client_id: str | None = None,
        client_secret: str | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        session: requests.Session | None = None,
    ) -> None:
        """Initialise the provider.

        Credentials fall back to the ``DIGIKEY_CLIENT_ID`` and
        ``DIGIKEY_CLIENT_SECRET`` environment variables when not supplied.

        Args:
            client_id: OAuth2 client identifier.
            client_secret: OAuth2 client secret.
            timeout: Per-request timeout in seconds.
            session: Optional pre-built session, primarily for tests.
        """
        self.client_id = client_id or os.environ.get("DIGIKEY_CLIENT_ID", "")
        self.client_secret = client_secret or os.environ.get("DIGIKEY_CLIENT_SECRET", "")
        self.timeout = timeout
        self._session = session if session is not None else requests.Session()
        self._owns_session = session is None
        self._token: str | None = None
        #: Monotonic timestamp of the last product request, for pacing.
        self._last_request: float | None = None
        #: HTTP requests actually issued, which is what the quota counts.
        self._http_requests = 0
        self._token_expiry: float = 0.0

    # ------------------------------------------------------------------
    # BaseProvider contract
    # ------------------------------------------------------------------
    def authenticate(self) -> None:
        """Fetch an OAuth2 access token, reusing a cached one when still valid.

        Raises:
            ConfigurationError: If client credentials are absent.
            ProviderAPIError: If the token endpoint rejects the request.
        """
        if not self.client_id or not self.client_secret:
            raise ConfigurationError(
                "DigiKey credentials are missing. Set DIGIKEY_CLIENT_ID and "
                "DIGIKEY_CLIENT_SECRET, or pass --client-id/--client-secret."
            )

        if self._token and time.time() < self._token_expiry:
            LOGGER.debug("Reusing cached DigiKey access token")
            return

        credentials = base64.b64encode(
            f"{self.client_id}:{self.client_secret}".encode()
        ).decode()

        self._http_requests += 1
        try:
            response = self._session.post(
                TOKEN_URL,
                data={"grant_type": "client_credentials"},
                headers={
                    "Authorization": f"Basic {credentials}",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise ProviderAPIError(f"DigiKey token request failed: {exc}") from exc

        if response.status_code != 200:
            raise ProviderAPIError(
                f"DigiKey authentication failed with HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )

        try:
            payload: dict[str, Any] = response.json()
            self._token = str(payload["access_token"])
            self._token_expiry = time.time() + float(payload.get("expires_in", 1800)) - 60
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderAPIError(f"Malformed DigiKey token response: {exc}") from exc

        LOGGER.debug("Obtained DigiKey access token")

    def fetch_component_data(self, mpn: str) -> ComponentData:
        """Look up one manufacturer part number.

        Args:
            mpn: The manufacturer part number to resolve.

        Returns:
            Normalised component data.

        Raises:
            PartNotFoundError: If DigiKey returns no exact MPN match.
            ProviderAPIError: If the search request fails.
        """
        self.authenticate()
        assert self._token is not None  # narrowed by authenticate()

        headers = {
            "Authorization": f"Bearer {self._token}",
            "X-DIGIKEY-Client-Id": self.client_id,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        match = self._search(mpn, headers)
        if match is None:
            raise PartNotFoundError(mpn)
        return self._to_component_data(mpn, match)

    def _pace(self) -> None:
        """Sleep if the previous request was too recent.

        Keeps a run under the per-minute ceiling without relying on the API to
        refuse work. The interval is measured between request *starts*, so the
        pacing holds even when a response is slow.
        """
        if self._last_request is None:
            self._last_request = time.monotonic()
            return
        elapsed = time.monotonic() - self._last_request
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        self._last_request = time.monotonic()

    @staticmethod
    def _describe_error(response: requests.Response) -> str:
        """Return a readable one line description of a failed response.

        DigiKey returns a JSON:API problem document, so echoing the raw body
        prints several hundred characters of punctuation and hides the one fact
        that matters.

        Args:
            response: The failed response.

        Returns:
            A single line naming the status and the reason.
        """
        try:
            payload = response.json()
        except ValueError:
            text = " ".join(response.text.split())[:160]
            return f"HTTP {response.status_code}: {text}" if text else (
                f"HTTP {response.status_code}"
            )
        if not isinstance(payload, dict):  # pragma: no cover - defensive
            return f"HTTP {response.status_code}"
        for key in ("detail", "ErrorMessage", "message", "title"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return f"HTTP {response.status_code}: {' '.join(value.split())}"
        return f"HTTP {response.status_code}"

    @staticmethod
    def _rate_limit(response: requests.Response) -> tuple[bool, int, str]:
        """Classify a 429 as a daily quota or a per-minute burst.

        Args:
            response: The 429 response.

        Returns:
            A ``(daily, retry_after, resets_at)`` triple.
        """
        headers = response.headers
        try:
            retry_after = int(headers.get("Retry-After", "0"))
        except (TypeError, ValueError):  # pragma: no cover - malformed header
            retry_after = 0
        remaining = headers.get("X-RateLimit-Remaining")
        resets_at = headers.get("X-RateLimit-ResetTime", "") or headers.get(
            "X-BurstLimit-ResetTime", ""
        )
        # A daily refusal is the one that reports zero remaining for the whole
        # day window; a burst reports the per-minute counters instead.
        daily = remaining is not None and remaining.strip() == "0"
        return daily, retry_after, resets_at

    def _post(
        self, headers: dict[str, str | None], body: dict[str, Any], mpn: str
    ) -> requests.Response:
        """POST to the search endpoint, pacing and retrying a burst refusal.

        Args:
            headers: Authenticated request headers.
            body: JSON request body.
            mpn: Part number being looked up, used in error messages.

        Returns:
            A response whose status is not a rate limit refusal.

        Raises:
            RateLimitError: If the daily quota is exhausted, or a burst refusal
                survives every retry.
            ProviderAPIError: On a transport failure.
        """
        attempt = 0
        while True:
            self._pace()
            self._http_requests += 1
            try:
                response = self._session.post(
                    PRODUCT_URL, headers=headers, json=body, timeout=self.timeout
                )
            except requests.RequestException as exc:
                raise ProviderAPIError(f"DigiKey search failed for {mpn!r}: {exc}") from exc

            if response.status_code != 429:
                return response

            daily, retry_after, resets_at = self._rate_limit(response)
            when = resets_at or f"in {retry_after}s" if retry_after else "shortly"
            if daily:
                raise RateLimitError(
                    f"DigiKey daily request quota exhausted (1000 per day). "
                    f"Resets at {when}. The run stopped rather than waiting; "
                    f"re-run after the quota returns.",
                    retry_after=retry_after,
                    resets_at=resets_at,
                    daily=True,
                )

            attempt += 1
            if attempt > _MAX_BURST_RETRIES:
                raise RateLimitError(
                    f"DigiKey per-minute rate limit still refusing after "
                    f"{_MAX_BURST_RETRIES} retries.",
                    retry_after=retry_after,
                    resets_at=resets_at,
                )
            wait = min(
                float(retry_after) if retry_after else _INITIAL_BACKOFF * (2 ** (attempt - 1)),
                _MAX_BACKOFF,
            )
            LOGGER.warning(
                "DigiKey burst limit hit for %r; waiting %.1fs (retry %d/%d)",
                mpn,
                wait,
                attempt,
                _MAX_BURST_RETRIES,
            )
            time.sleep(wait)

    def _search(self, mpn: str, headers: dict[str, str | None]) -> dict[str, Any] | None:
        """Return the product whose MPN matches ``mpn`` exactly, paging if needed.

        The first page is always fetched, so a distinctive part number costs one
        request. Only when no exact match is present does the search walk further
        pages, up to :data:`_MAX_SEARCH_PAGES`.

        Args:
            mpn: The manufacturer part number to look for.
            headers: Authenticated request headers.

        Returns:
            The matching product record, or ``None`` when none was found.

        Raises:
            PartNotFoundError: If the API itself reports the path as unknown.
            ProviderAPIError: If any request fails.
        """
        for page in range(_MAX_SEARCH_PAGES):
            body = {
                "Keywords": mpn,
                "Limit": _SEARCH_LIMIT,
                "Offset": page * _SEARCH_LIMIT,
            }
            response = self._post(headers, body, mpn)

            if response.status_code == 404:
                raise PartNotFoundError(mpn)
            if response.status_code != 200:
                raise ProviderAPIError(
                    f"DigiKey search for {mpn!r} failed: "
                    f"{self._describe_error(response)}"
                )

            try:
                payload: dict[str, Any] = response.json()
            except ValueError as exc:
                raise ProviderAPIError(
                    f"Malformed DigiKey response for {mpn!r}: {exc}"
                ) from exc

            products = payload.get("Products") or []
            match = self._select_exact(products, mpn)
            if match is not None:
                if page:
                    LOGGER.debug(
                        "Found exact match for %r on page %d of DigiKey results", mpn, page + 1
                    )
                return match

            if len(products) < _SEARCH_LIMIT:
                # A short page is the last page; there is nothing more to fetch.
                break
        return None

    @property
    def http_requests(self) -> int:
        """Return how many product requests have been issued."""
        return self._http_requests

    def close(self) -> None:
        """Close the underlying HTTP session if this provider created it."""
        if self._owns_session:
            self._session.close()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _select_exact(products: list[dict[str, Any]], mpn: str) -> dict[str, Any] | None:
        """Return the product whose MPN matches exactly, else ``None``.

        Args:
            products: Candidate products from the search response.
            mpn: The requested manufacturer part number.

        Returns:
            The exact match, or ``None`` when only fuzzy results were returned.
        """
        wanted = mpn.strip().casefold()
        for product in products:
            candidate = str(product.get("ManufacturerProductNumber", "")).strip().casefold()
            if candidate == wanted:
                return product
        return None

    @staticmethod
    def _to_component_data(mpn: str, product: dict[str, Any]) -> ComponentData:
        """Map a DigiKey product payload onto :class:`ComponentData`.

        Args:
            mpn: The requested manufacturer part number.
            product: The matched product record.

        Returns:
            Normalised component data, with every vendor parameter preserved
            in ``raw_parameters``.
        """
        parameters: dict[str, str] = {}
        for entry in product.get("Parameters", []) or []:
            # The API names these ParameterText/ValueText. Reading "Parameter"
            # and "Value" returns nothing at all, which left every part with an
            # empty parameter set and therefore no Value and no ranges.
            name = str(entry.get("ParameterText", "")).strip()
            value = str(entry.get("ValueText", "")).strip()
            if name and not _is_placeholder(value):
                parameters[name] = value

        manufacturer = product.get("Manufacturer", {}) or {}
        voltage = _build_bounds(_first_match(parameters, _VOLTAGE_KEYS))
        temperature = _build_bounds(_first_match(parameters, _TEMP_KEYS))
        return ComponentData(
            mpn=mpn,
            manufacturer=str(manufacturer.get("Name", "")).strip(),
            description=description_of(product),
            datasheet=datasheet_of(product),
            value=derive_value(parameters) if is_discrete_passive(product) else "",
            voltage_min=voltage[0],
            voltage_max=voltage[1],
            voltage_text=voltage[2],
            temp_min=temperature[0],
            temp_max=temperature[1],
            temp_text=temperature[2],
            package=_first_match(parameters, _PACKAGE_KEYS),
            digikey_url=product_url(product),
            raw_parameters=parameters,
        )


def is_discrete_passive(product: dict[str, Any]) -> bool:
    """Return whether a product's ``Value`` should be derived from parameters.

    Only the discrete passive families qualify. Everything else, including
    modules and integrated circuits, conventionally carries its part number as
    the Value, and deriving one would destroy it.

    Args:
        product: The matched product record.

    Returns:
        True when the product is a capacitor, resistor, inductor or crystal.
    """
    category = product.get("Category")
    if not isinstance(category, dict):
        return False
    name = str(category.get("Name", "")).strip().casefold()
    return name in _PASSIVE_CATEGORIES


def product_url(product: dict[str, Any]) -> str:
    """Return the DigiKey product page URL exactly as the API supplied it.

    No URL is synthesised. A hand-built URL is indistinguishable from a real one
    in the symbol library, so a missing value is reported as missing rather than
    filled with something that merely looks right.

    Args:
        product: The matched product record.

    Returns:
        The URL from the API, or ``""`` when the response omitted it.
    """
    return str(product.get("ProductUrl", "")).strip()


def description_of(product: dict[str, Any]) -> str:
    """Return the human readable description.

    ``Description`` in this API is an object, not a string. ``DetailedDescription``
    is preferred because it is the full prose form, for example
    ``"10 kOhms ±1% 0.063W Chip Resistor 0402 (1005 Metric)"``; the short
    ``ProductDescription`` is the fallback, and some entries carry neither.

    Args:
        product: The matched product record.

    Returns:
        The description, or ``""`` when the record carried none.
    """
    description = product.get("Description")
    if isinstance(description, str):
        return description.strip()
    if isinstance(description, dict):
        for key in ("DetailedDescription", "ProductDescription"):
            value = description.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
    return ""


def datasheet_of(product: dict[str, Any]) -> str:
    """Return the datasheet URL.

    The field is named ``DatasheetUrl``, and DigiKey frequently returns a
    protocol-relative value such as ``//mm.digikey.com/...``. KiCad stores this
    string verbatim, so an unqualified ``//host/...`` would not resolve when
    clicked; the scheme is restored in that case and only then.

    Args:
        product: The matched product record.

    Returns:
        An absolute datasheet URL, or ``""`` when the record carried none.
    """
    url = str(product.get("DatasheetUrl", "")).strip()
    if url.startswith("//"):
        return f"https:{url}"
    return url


#: Values DigiKey uses to mean "this parameter is not applicable".
#:
#: The API fills inapplicable parameters with a bare hyphen rather than omitting
#: them. Writing that through would put a meaningless ``Operating Temperature =
#: "-"`` into the symbol library, so it is treated as absent.
_PLACEHOLDER_VALUES = frozenset({"-", "--", "n/a", "na", "none", "not applicable", ""})


def _is_placeholder(text: str) -> bool:
    """Return whether a vendor value carries no information.

    Args:
        text: A raw parameter value as the vendor wrote it.

    Returns:
        True when the value is one of the API's "not applicable" markers.
    """
    return text.strip().casefold() in _PLACEHOLDER_VALUES


def _build_bounds(
    text: str,
) -> tuple[str, str, str]:
    """Turn a vendor limit string into ``(min, max, verbatim_text)``.

    Both bounds are emitted only when the source actually contained two values.
    A single value keeps the original string so no information is lost and no
    bound is invented.

    Args:
        text: The raw vendor parameter value.

    Returns:
        A ``(min, max, text)`` triple; empty strings where nothing was derived.
    """
    if _is_placeholder(text):
        return ("", "", "")
    bounds = parse_range(text)
    if bounds is None or not bounds.has_bounds:
        return ("", "", text.strip())
    unit = bounds.unit or ""
    low = ComponentData._fmt_bound(bounds.low or 0.0, unit, temperature=bounds.is_temperature)
    high = ComponentData._fmt_bound(bounds.high or 0.0, unit, temperature=bounds.is_temperature)
    if not low or not high:
        return ("", "", text.strip())
    return (low, high, "")


def _first_match(parameters: dict[str, str], keys: tuple[str, ...]) -> str:
    """Return the first non-empty value whose key matches, ignoring punctuation.

    DigiKey parameter names are inconsistent: ``"Voltage - Rated"``,
    ``"Voltage - Supply"`` and ``"Package / Case"`` all mix spaces, hyphens and
    slashes. Replacing each character individually produced ``"voltage___supply"``
    and ``"package_/_case"``, which matched nothing and left every part with an
    empty package and no voltage. Runs of separator characters are therefore
    collapsed to a single underscore, so all of those normalise to
    ``voltage_supply`` and ``package_case``.

    Args:
        parameters: Vendor parameter mapping.
        keys: Candidate keys in priority order.

    Returns:
        The matching value, or ``""`` when nothing matches.
    """
    lookup = {_normalise_key(key): value for key, value in parameters.items()}
    for key in keys:
        value = lookup.get(_normalise_key(key), "").strip()
        if value:
            return value
    return ""


def _normalise_key(text: str) -> str:
    """Return a canonical form of a vendor parameter name for comparison.

    Args:
        text: A parameter name as the vendor wrote it.

    Returns:
        The name lowercased with every run of spaces, hyphens, slashes and
        underscores collapsed to a single underscore.
    """
    return _SEPARATORS.sub("_", text.strip().casefold()).strip("_")
