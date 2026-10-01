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
import time
from typing import Any

import requests

from ..exceptions import ConfigurationError, PartNotFoundError, ProviderAPIError
from ..models import ComponentData
from .base import BaseProvider

__all__ = ["DigiKeyProvider"]

LOGGER = logging.getLogger(__name__)

TOKEN_URL = "https://api.digikey.com/v1/oauth2/token"
PRODUCT_URL = "https://api.digikey.com/products/v4/search/{keyword}"
DEFAULT_TIMEOUT = 30.0

#: Parameter names normalised onto dedicated :class:`ComponentData` attributes.
_TEMP_KEYS = ("operating_temperature", "temperature_range", "operating_temp")
_VOLTAGE_KEYS = ("voltage_rating", "voltage", "supply_voltage")


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

        url = PRODUCT_URL.format(keyword=mpn)
        headers = {"Authorization": f"Bearer {self._token}", "X-DIGIKEY-Client-Id": self.client_id}

        try:
            response = self._session.get(url, headers=headers, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ProviderAPIError(f"DigiKey search failed for {mpn!r}: {exc}") from exc

        if response.status_code == 404:
            raise PartNotFoundError(mpn)
        if response.status_code != 200:
            raise ProviderAPIError(
                f"DigiKey search for {mpn!r} failed with HTTP {response.status_code}"
            )

        try:
            payload: dict[str, Any] = response.json()
        except ValueError as exc:
            raise ProviderAPIError(f"Malformed DigiKey response for {mpn!r}: {exc}") from exc

        products = payload.get("Products") or []
        match = self._select_exact(products, mpn)
        if match is None:
            raise PartNotFoundError(mpn)

        return self._to_component_data(mpn, match)

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
            name = str(entry.get("Parameter", "")).strip()
            value = str(entry.get("Value", "")).strip()
            if name and value:
                parameters[name] = value

        manufacturer = product.get("Manufacturer", {}) or {}
        return ComponentData(
            mpn=mpn,
            manufacturer=str(manufacturer.get("Name", "")).strip(),
            description=str(product.get("Description", "")).strip(),
            voltage=_first_match(parameters, _VOLTAGE_KEYS),
            operating_temp=_first_match(parameters, _TEMP_KEYS),
            package=str(
                (product.get("Package") or {}).get("Name", "")
            ).strip(),
            raw_parameters=parameters,
        )


def _first_match(parameters: dict[str, str], keys: tuple[str, ...]) -> str:
    """Return the first non-empty value whose key matches, ignoring case and spacing.

    DigiKey spells parameter names with spaces (``"Voltage Rating"``) while
    other catalogues use underscores, so both sides are normalised before
    comparison.

    Args:
        parameters: Vendor parameter mapping.
        keys: Candidate keys in priority order.

    Returns:
        The matching value, or ``""`` when nothing matches.
    """

    def normalise(text: str) -> str:
        return text.strip().casefold().replace(" ", "_").replace("-", "_")

    lookup = {normalise(key): value for key, value in parameters.items()}
    for key in keys:
        value = lookup.get(normalise(key), "").strip()
        if value:
            return value
    return ""
