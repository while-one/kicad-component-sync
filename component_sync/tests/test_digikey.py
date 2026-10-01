"""Mocked HTTP tests for :class:`DigiKeyProvider`.

No test in this module performs real network I/O. Every request is intercepted
with :mod:`unittest.mock`, so the suite runs offline and deterministically.
"""

from __future__ import annotations

import base64
import json
from typing import Any, cast
from unittest import mock

import pytest
import requests

from component_sync.exceptions import (
    ConfigurationError,
    PartNotFoundError,
    ProviderAPIError,
)
from component_sync.models import ComponentData
from component_sync.providers.digikey import PRODUCT_URL, TOKEN_URL, DigiKeyProvider

MPN = "GRM155R61C104KA88D"

TOKEN_PAYLOAD: dict[str, Any] = {
    "access_token": "tok-123",
    "token_type": "Bearer",
    "expires_in": 1800,
}

PRODUCT_PAYLOAD: dict[str, Any] = {
    "Products": [
        {
            "ManufacturerProductNumber": "SOMETHING-ELSE",
            "Description": "Fuzzy result that must be ignored",
        },
        {
            "ManufacturerProductNumber": MPN,
            "Description": "Multilayer Ceramic Capacitors MLCC - 0.1uF",
            "Manufacturer": {"Name": "Murata"},
            "Package": {"Name": "0402"},
            "Parameters": [
                {"Parameter": "Voltage Rating", "Value": "16 VDC"},
                {"Parameter": "Operating Temperature", "Value": "-55 C / +85 C"},
                {"Parameter": "Capacitance", "Value": "0.1 uF"},
            ],
        },
    ]
}


def make_provider(**kwargs: Any) -> DigiKeyProvider:
    """Build a provider with stub credentials.

    Args:
        **kwargs: Overrides passed to the constructor.

    Returns:
        A configured provider using a fresh mocked session.
    """
    session = mock.MagicMock(spec=requests.Session)
    options: dict[str, Any] = {
        "client_id": "client",
        "client_secret": "secret",
        "session": session,
    }
    options.update(kwargs)
    return DigiKeyProvider(**options)



def session_of(provider: DigiKeyProvider) -> mock.MagicMock:
    """Return a provider's session as a mock, so calls can be asserted.

    ``DigiKeyProvider`` stores its session as a ``requests.Session`` for type
    checking, but tests always inject a ``MagicMock``; this accessor narrows it
    back without weakening the provider's annotations.

    Args:
        provider: The provider under test.

    Returns:
        The underlying mock session.
    """
    return cast(mock.MagicMock, provider._session)


def response(status: int = 200, payload: Any = None) -> mock.MagicMock:
    """Build a mock HTTP response.

    Args:
        status: HTTP status code.
        payload: JSON payload, or ``None`` to make ``.json()`` raise.

    Returns:
        A mock response object.
    """
    resp = mock.MagicMock()
    resp.status_code = status
    resp.text = "error body"
    if payload is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


class TestAuthentication:
    """Verify OAuth2 token handling."""

    def test_missing_credentials_raise(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Absent credentials are a configuration error."""
        monkeypatch.delenv("DIGIKEY_CLIENT_ID", raising=False)
        monkeypatch.delenv("DIGIKEY_CLIENT_SECRET", raising=False)
        provider = DigiKeyProvider(session=mock.MagicMock())
        with pytest.raises(ConfigurationError, match="credentials are missing"):
            provider.authenticate()

    def test_token_request_uses_basic_auth(self) -> None:
        """The token call sends a Basic authorization header."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, TOKEN_PAYLOAD)
        provider.authenticate()

        _, kwargs = session_of(provider).post.call_args
        expected = base64.b64encode(b"client:secret").decode()
        assert kwargs["headers"]["Authorization"] == f"Basic {expected}"
        assert session_of(provider).post.call_args[0][0] == TOKEN_URL
        assert kwargs["data"] == {"grant_type": "client_credentials"}

    def test_token_is_cached(self) -> None:
        """A second authenticate reuses the cached token."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, TOKEN_PAYLOAD)
        provider.authenticate()
        provider.authenticate()
        assert session_of(provider).post.call_count == 1

    def test_http_error_raises_provider_error(self) -> None:
        """A rejected token request raises ProviderAPIError."""
        provider = make_provider()
        session_of(provider).post.return_value = response(401)
        with pytest.raises(ProviderAPIError, match="authentication failed"):
            provider.authenticate()

    def test_transport_error_raises_provider_error(self) -> None:
        """A connection failure is wrapped in ProviderAPIError."""
        provider = make_provider()
        session_of(provider).post.side_effect = requests.ConnectionError("boom")
        with pytest.raises(ProviderAPIError, match="token request failed"):
            provider.authenticate()

    def test_malformed_token_payload_raises(self) -> None:
        """A token response without access_token is rejected."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, {"unexpected": 1})
        with pytest.raises(ProviderAPIError, match="Malformed"):
            provider.authenticate()

    def test_expired_token_is_refetched(self) -> None:
        """An expired token triggers a fresh token request."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, TOKEN_PAYLOAD)
        provider.authenticate()
        provider._token_expiry = 0.0
        provider.authenticate()
        assert session_of(provider).post.call_count == 2


class TestFetchComponentData:
    """Verify part lookup and payload mapping."""

    def _authenticated(self, product_response: Any) -> DigiKeyProvider:
        """Return a provider whose token call succeeds.

        Args:
            product_response: Response returned for the product search.

        Returns:
            A provider ready for a product lookup.
        """
        provider = make_provider()
        session_of(provider).post.return_value = response(200, TOKEN_PAYLOAD)
        session_of(provider).get.return_value = product_response
        return provider

    def test_fetch_returns_component_data(self) -> None:
        """A successful lookup yields normalised data."""
        provider = self._authenticated(response(200, PRODUCT_PAYLOAD))
        data = provider.fetch_component_data(MPN)

        assert isinstance(data, ComponentData)
        assert data.mpn == MPN
        assert data.manufacturer == "Murata"
        assert data.package == "0402"
        assert data.description.startswith("Multilayer Ceramic")

    def test_range_parameter_is_split_into_bounds(self) -> None:
        """A '16 VDC' style single value is preserved verbatim, not split."""
        provider = self._authenticated(response(200, PRODUCT_PAYLOAD))
        data = provider.fetch_component_data(MPN)
        assert data.voltage_text == "16 VDC"
        assert data.voltage_min == ""
        assert data.voltage_max == ""
        assert data.as_properties()["Voltage Rating"] == "16 VDC"

    def test_temperature_range_is_split_into_bounds(self) -> None:
        """A '-55 C / +85 C' style range becomes two discrete fields."""
        payload = json.loads(json.dumps(PRODUCT_PAYLOAD))
        payload["Products"][1]["Parameters"].append(
            {"Parameter": "Operating Temperature", "Value": "-55 C / +150 C"}
        )
        # the fixture already carries a single -55/+85 value; replace it
        payload["Products"][1]["Parameters"] = [
            {"Parameter": "Voltage Rating", "Value": "16 VDC"},
            {"Parameter": "Operating Temperature", "Value": "-55 C / +150 C"},
        ]
        provider = self._authenticated(response(200, payload))
        data = provider.fetch_component_data(MPN)
        assert data.temp_min == "-55 C"
        assert data.temp_max == "+150 C"
        assert data.temp_text == ""
        properties = data.as_properties()
        assert properties["Temperature Min"] == "-55 C"
        assert properties["Temperature Max"] == "+150 C"
        assert "Operating Temperature" not in properties

    def test_voltage_range_uses_min_max_fields(self) -> None:
        """A '2.65 V to 3.6 V' style range becomes Voltage Min/Max."""
        payload = json.loads(json.dumps(PRODUCT_PAYLOAD))
        payload["Products"][1]["Parameters"] = [
            {"Parameter": "Voltage", "Value": "2.65 V to 3.6 V"},
        ]
        provider = self._authenticated(response(200, payload))
        data = provider.fetch_component_data(MPN)
        assert data.voltage_min == "2.65 V"
        assert data.voltage_max == "3.6 V"
        properties = data.as_properties()
        assert properties["Voltage Min"] == "2.65 V"
        assert properties["Voltage Max"] == "3.6 V"

    def test_raw_parameters_are_preserved(self) -> None:
        """Unmapped vendor parameters survive in raw_parameters."""
        provider = self._authenticated(response(200, PRODUCT_PAYLOAD))
        data = provider.fetch_component_data(MPN)
        assert data.raw_parameters["Capacitance"] == "0.1 uF"

    def test_exact_match_wins_over_fuzzy(self) -> None:
        """A case-different but exact MPN match is selected."""
        provider = self._authenticated(response(200, PRODUCT_PAYLOAD))
        data = provider.fetch_component_data(MPN.lower())
        assert data.manufacturer == "Murata"

    def test_bearer_token_sent_on_search(self) -> None:
        """The search call carries the bearer token and client id."""
        provider = self._authenticated(response(200, PRODUCT_PAYLOAD))
        provider.fetch_component_data(MPN)
        _, kwargs = session_of(provider).get.call_args
        assert kwargs["headers"]["Authorization"] == "Bearer tok-123"
        assert kwargs["headers"]["X-DIGIKEY-Client-Id"] == "client"
        assert session_of(provider).get.call_args[0][0] == PRODUCT_URL.format(keyword=MPN)

    def test_only_fuzzy_results_raise_not_found(self) -> None:
        """Results without an exact MPN are treated as a miss."""
        payload = {"Products": [PRODUCT_PAYLOAD["Products"][0]]}
        provider = self._authenticated(response(200, payload))
        with pytest.raises(PartNotFoundError, match=MPN):
            provider.fetch_component_data(MPN)

    def test_empty_product_list_raises_not_found(self) -> None:
        """An empty result set is a miss."""
        provider = self._authenticated(response(200, {"Products": []}))
        with pytest.raises(PartNotFoundError):
            provider.fetch_component_data(MPN)

    def test_404_raises_not_found(self) -> None:
        """HTTP 404 from the API is a miss, not an API error."""
        provider = self._authenticated(response(404))
        with pytest.raises(PartNotFoundError):
            provider.fetch_component_data(MPN)

    def test_500_raises_provider_error(self) -> None:
        """A server error is reported as a provider failure."""
        provider = self._authenticated(response(500))
        with pytest.raises(ProviderAPIError, match="HTTP 500"):
            provider.fetch_component_data(MPN)

    def test_transport_error_during_search(self) -> None:
        """A connection failure during search is wrapped."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, TOKEN_PAYLOAD)
        session_of(provider).get.side_effect = requests.Timeout("slow")
        with pytest.raises(ProviderAPIError, match="search failed"):
            provider.fetch_component_data(MPN)

    def test_malformed_json_raises(self) -> None:
        """A non-JSON body is reported as a provider error."""
        provider = self._authenticated(response(200, None))
        with pytest.raises(ProviderAPIError, match="Malformed"):
            provider.fetch_component_data(MPN)

    def test_missing_parameters_are_tolerated(self) -> None:
        """A product with no Parameters block still maps cleanly."""
        payload = {
            "Products": [
                {
                    "ManufacturerProductNumber": MPN,
                    "Description": "",
                    "Manufacturer": {"Name": "ACME"},
                    "Package": {"Name": "0603"},
                    "Parameters": [],
                }
            ]
        }
        provider = self._authenticated(response(200, payload))
        data = provider.fetch_component_data(MPN)
        assert data.voltage_min == ""
        assert data.voltage_max == ""
        assert data.voltage_text == ""
        assert data.temp_min == ""
        assert data.temp_max == ""
        assert data.package == "0603"


class TestSessionLifecycle:
    """Verify connection pooling and cleanup."""

    def test_owned_session_is_closed(self) -> None:
        """A session created by the provider is closed with it."""
        session = mock.MagicMock(spec=requests.Session)
        provider = DigiKeyProvider(
            client_id="a", client_secret="b", session=session
        )
        provider._owns_session = True
        provider.close()
        session.close.assert_called_once()

    def test_injected_session_is_not_closed(self) -> None:
        """An injected session is left for its owner to close."""
        session = mock.MagicMock(spec=requests.Session)
        provider = DigiKeyProvider(client_id="a", client_secret="b", session=session)
        provider.close()
        session.close.assert_not_called()

    def test_context_manager_closes(self) -> None:
        """Using the provider as a context manager closes it."""
        session = mock.MagicMock(spec=requests.Session)
        provider = DigiKeyProvider(client_id="a", client_secret="b", session=session)
        provider._owns_session = True
        with provider as entered:
            assert entered is provider
        session.close.assert_called_once()

    def test_single_session_reused(self) -> None:
        """Connection pooling: one session serves every lookup."""
        provider = self_authenticated_provider()
        provider.fetch_component_data(MPN)
        provider.fetch_component_data(MPN)
        assert session_of(provider).post.call_count == 1
        assert session_of(provider).get.call_count == 2


def self_authenticated_provider() -> DigiKeyProvider:
    """Return a provider wired for successful lookups.

    Returns:
        A provider with mocked session responses.
    """
    provider = make_provider()
    session_of(provider).post.return_value = response(200, TOKEN_PAYLOAD)
    session_of(provider).get.return_value = response(200, PRODUCT_PAYLOAD)
    return provider
