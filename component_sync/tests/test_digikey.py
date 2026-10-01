"""Tests for :class:`DigiKeyProvider`.

No test performs real network I/O. Requests are intercepted with
:mod:`unittest.mock`, so the suite runs offline and deterministically.

The product payloads are **captured from the live API**, not invented, and live
in ``fixtures/digikey_search.json``. That distinction is the whole point of this
module: an earlier version of these tests used a hand-written payload whose
parameter keys were ``Parameter``/``Value``, while the real API uses
``ParameterText``/``ValueText``. Every assertion passed and the provider was
nonetheless broken against the real service, because a fabricated fixture only
ever proves the code agrees with the author. Each regression below exists because
it was found by calling the live endpoint.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
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

#: Must match ``_MAX_SEARCH_PAGES`` in the provider.
_MAX_PAGES = 4

TOKEN_PAYLOAD: dict[str, Any] = {
    "access_token": "tok-123",
    "token_type": "Bearer",
    "expires_in": 1800,
}

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "digikey_search.json"

#: Real search responses, keyed by the part number that was requested.
CAPTURED: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def search_payload(mpn: str) -> dict[str, Any]:
    """Return the captured search response for ``mpn``.

    Args:
        mpn: A part number present in the fixture.

    Returns:
        The recorded response body, shaped as the API returned it.

    Raises:
        KeyError: If the part number was never captured.
    """
    entry = CAPTURED[mpn]
    product = entry.get("_product")
    products = [product] if product is not None else []
    return {"Products": products, "ProductsCount": entry.get("_productsCount")}



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


class CapturedPayloads:
    """Base class providing lookups against recorded API responses."""

    def _authenticated(self, product_response: Any) -> DigiKeyProvider:
        """Return a provider whose token and search calls are both mocked.

        Both the OAuth token exchange and the product search are POST requests,
        so the two responses are distinguished by URL rather than by method.

        Args:
            product_response: Response returned for the product search.

        Returns:
            A provider ready for a product lookup.
        """
        provider = make_provider()

        def dispatch(url: str, **_: Any) -> Any:
            return response(200, TOKEN_PAYLOAD) if url == TOKEN_URL else product_response

        session_of(provider).post.side_effect = dispatch
        return provider

    def _fetch(self, mpn: str) -> ComponentData:
        """Return the data the provider derives from a captured response.

        Args:
            mpn: Part number to look up.

        Returns:
            The normalised component data.
        """
        provider = self._authenticated(response(200, search_payload(mpn)))
        return provider.fetch_component_data(mpn)


class TestMappingAgainstCapturedResponses(CapturedPayloads):
    """Pin the field mapping to what DigiKey actually returns.

    Every expectation here was read off a live response, so these tests fail if
    the provider drifts away from the real contract.
    """

    def test_capacitor(self) -> None:
        """A Murata MLCC maps to SI value, package, rating and temperature."""
        data = self._fetch("GRM155R61C104KA88D")
        assert data.value == "100 nF"
        assert data.manufacturer == "Murata Electronics"
        assert data.package == "0402 (1005 Metric)"
        assert data.voltage_text == "16V"
        assert data.voltage_min == "" and data.voltage_max == ""
        assert data.temp_min == "-55 C"
        assert data.temp_max == "+85 C"
        assert data.raw_parameters["Capacitance"] == "0.1 \u00b5F"

    def test_capacitor_description_is_prose_not_a_dict(self) -> None:
        """``Description`` is an object in this API, and must not be stringified.

        Reading it directly produced a Python dict repr in the Description field
        of the symbol library.
        """
        data = self._fetch("GRM155R61C104KA88D")
        assert data.description == (
            "0.1 \u00b5F \u00b110% 16V Ceramic Capacitor X5R 0402 (1005 Metric)"
        )
        assert "{" not in data.description
        assert "ProductDescription" not in data.description

    def test_protocol_relative_datasheet_becomes_absolute(self) -> None:
        """A ``//host/...`` datasheet is unusable verbatim in a symbol library."""
        data = self._fetch("GRM155R61C104KA88D")
        assert data.datasheet.startswith("https://mm.digikey.com/")

    def test_resistor_uses_supplier_package_when_present(self) -> None:
        """``Package / Case`` is preferred over ``Supplier Device Package``."""
        data = self._fetch("RC0402FR-0710KL")
        assert data.package == "0402 (1005 Metric)"
        assert data.value == "10 kOhm"

    def test_resistor_of_36k5_is_36_5_kilo_ohm(self) -> None:
        """DigiKey reports 36.5 kOhms, and 36.5 kOhm is a valid E96 value.

        The library label ``36k5`` reads as 36.5 kOhms and is therefore already
        correct; only its format needed changing.
        """
        data = self._fetch("RC0402FR-0736K5L")
        assert data.value == "36.5 kOhm"
        assert data.raw_parameters["Resistance"] == "36.5 kOhms"
        assert data.raw_parameters["Tolerance"] == "\u00b11%"

    def test_inductor(self) -> None:
        """An inductor maps to package and temperature but no voltage."""
        data = self._fetch("MLJ1608WR56KT000")
        assert data.value == "560 nH"
        assert data.package == "0603 (1608 Metric)"
        assert data.temp_min == "-55 C" and data.temp_max == "+125 C"
        assert data.voltage_text == ""

    def test_crystal_frequency(self) -> None:
        """A crystal derives its Value from the Frequency parameter."""
        data = self._fetch("CX3225GA16000D0PTVCC")
        assert data.value == "16 MHz"
        assert data.temp_max == "+150 C"

    def test_tantalum_capacitor_voltage(self) -> None:
        """A rated voltage with a space after the number stays verbatim."""
        data = self._fetch("TAJB107M006RNJ")
        assert data.value == "100 uF"
        assert data.voltage_text == "6.3 V"
        assert data.package == "1411 (3528 Metric), 1210"

    def test_dual_voltage_range_takes_the_supply_rail(self) -> None:
        """A two-range supply string resolves to the first range, not a merge.

        ``1.65V ~ 1.95V, 3V ~ 3.6V`` describes two selectable rails. Reporting
        the first is honest; reporting 1.65 V to 3.6 V would invent a range that
        does not exist.
        """
        data = self._fetch("PN7160A1HN/C100E")
        assert data.voltage_min == "1.65 V"
        assert data.voltage_max == "1.95 V"

    def test_temperature_with_ta_suffix(self) -> None:
        """A ``(TA)`` suffix on the range does not defeat parsing."""
        data = self._fetch("TXS0108EPWR")
        assert data.temp_min == "-40 C"
        assert data.temp_max == "+85 C"


class TestPlaceholderValues(CapturedPayloads):
    """DigiKey marks inapplicable parameters with a bare hyphen.

    Writing that through would put a meaningless ``Operating Temperature = "-"``
    into the symbol library, which is worse than leaving the field absent.
    """

    def test_placeholder_is_dropped_from_parameters(self) -> None:
        """A ``-`` parameter is not preserved as if it were a real value."""
        data = self._fetch("GRM155R61C104KA88D")
        assert "-" not in data.raw_parameters.values()
        for key in ("Features", "Ratings", "Height - Seated (Max)", "Lead Spacing"):
            assert key not in data.raw_parameters, key

    def test_placeholder_temperature_is_not_written(self) -> None:
        """A part whose only temperature parameter is ``-`` gets no field."""
        product = json.loads(json.dumps(search_payload("GRM155R61C104KA88D")))
        product["Products"][0]["Parameters"] = [
            {
                "ParameterId": 252,
                "ParameterText": "Operating Temperature",
                "ParameterType": "String",
                "ValueId": "1",
                "ValueText": "-",
            }
        ]
        provider = self._authenticated(response(200, product))
        data = provider.fetch_component_data("GRM155R61C104KA88D")
        assert data.temp_min == "" and data.temp_max == "" and data.temp_text == ""
        assert "Operating Temperature" not in data.as_properties()
        assert "Temperature Min" not in data.as_properties()


class TestPassiveGate(CapturedPayloads):
    """A Value is only derived for discrete passives.

    Modules and integrated circuits conventionally carry their part number as the
    Value. The NFC reader ``PN7160A1HN/C100E`` publishes ``Frequency =
    13.56MHz``, so deriving from parameters alone would replace its part number
    with "13.56 MHz" and destroy the symbol's identity.
    """

    def test_nfc_reader_keeps_its_part_number(self) -> None:
        """An RF module is not a passive, so no Value is derived."""
        data = self._fetch("PN7160A1HN/C100E")
        assert data.value == ""
        assert data.raw_parameters["Frequency"] == "13.56MHz"
        assert "Value" not in data.as_properties()

    def test_cellular_module_keeps_its_part_number(self) -> None:
        """A modem publishes a frequency band but is still not a passive."""
        data = self._fetch("BG95M3LA-64-SGNS")
        assert data.value == ""

    def test_ic_gets_no_value_but_keeps_its_ranges(self) -> None:
        """An IC is left alone for Value while still gaining package and ranges."""
        data = self._fetch("TXS0108EPWR")
        assert data.value == ""
        assert data.package == '20-TSSOP (0.173", 4.40mm Width)'

    def test_switch_gets_no_value(self) -> None:
        """A switch is not a passive."""
        data = self._fetch("ATS2D3G NC LFG")
        assert data.value == ""

    def test_crystal_is_a_passive(self) -> None:
        """Crystals are discrete components whose Value is their frequency."""
        data = self._fetch("LFXTAL085849")
        assert data.value == "27.12 MHz"


class TestSearchPaging(CapturedPayloads):
    """A short part number must not be reported as unstocked.

    DigiKey's search is a fuzzy, tokenised match. The bare part number
    ``1028`` returns 19,963 candidate products, and the Keystone part of that
    number sits around position 50. Fetching only ten candidates per request put
    it outside the window, so a stocked part was reported as missing.
    """

    @staticmethod
    def _page(mpns: list[str], total: int = 19963) -> dict[str, Any]:
        """Return a search response carrying the given candidate MPNs.

        Args:
            mpns: Manufacturer part numbers present on this page.
            total: Total number of matches the API reports.

        Returns:
            A response body.
        """
        return {
            "ProductsCount": total,
            "Products": [
                {
                    "ManufacturerProductNumber": value,
                    "Manufacturer": {"Name": "Decoy"},
                    "Description": {"ProductDescription": f"filler {value}"},
                    "Parameters": [],
                }
                for value in mpns
            ],
        }

    def test_page_size_is_the_maximum_the_api_accepts(self) -> None:
        """Limit 50 is accepted; 51 and above are rejected with HTTP 400."""
        from component_sync.providers.digikey import _SEARCH_LIMIT

        assert _SEARCH_LIMIT == 50

    def test_exact_match_on_first_page_costs_one_request(self) -> None:
        """A distinctive part number resolves without paging."""
        calls: list[int] = []

        def dispatch(url: str, **kwargs: Any) -> Any:
            if url == TOKEN_URL:
                return response(200, TOKEN_PAYLOAD)
            calls.append(kwargs["json"]["Offset"])
            return response(200, self._page(["SOMETHING-ELSE", MPN]))

        provider = make_provider()
        session_of(provider).post.side_effect = dispatch
        data = provider.fetch_component_data(MPN)
        assert data.mpn == MPN
        assert calls == [0], "no extra request when page one already matches"

    def test_exact_match_found_on_a_later_page(self) -> None:
        """A part beyond the first window is found rather than called missing."""
        calls: list[int] = []
        fillers = [f"DECOY-{i}" for i in range(50)]

        def dispatch(url: str, **kwargs: Any) -> Any:
            if url == TOKEN_URL:
                return response(200, TOKEN_PAYLOAD)
            offset = kwargs["json"]["Offset"]
            calls.append(offset)
            if offset == 0:
                return response(200, self._page(fillers))
            return response(200, self._page([MPN]))

        provider = make_provider()
        session_of(provider).post.side_effect = dispatch
        data = provider.fetch_component_data(MPN)
        assert data.mpn == MPN
        assert calls == [0, 50], "paged forward exactly once"

    def test_paging_stops_on_a_short_final_page(self) -> None:
        """A partial page means there is nothing further to fetch."""
        calls: list[int] = []

        def dispatch(url: str, **kwargs: Any) -> Any:
            if url == TOKEN_URL:
                return response(200, TOKEN_PAYLOAD)
            offset = kwargs["json"]["Offset"]
            calls.append(offset)
            # Only three candidates, so this is the last page.
            return response(200, self._page(["A", "B", "C"], total=3))

        provider = make_provider()
        session_of(provider).post.side_effect = dispatch
        with pytest.raises(PartNotFoundError):
            provider.fetch_component_data(MPN)
        assert calls == [0], "did not request a page beyond a short response"

    def test_paging_is_bounded(self) -> None:
        """An endless run of full pages is abandoned, not followed forever."""

        def dispatch(url: str, **kwargs: Any) -> Any:
            if url == TOKEN_URL:
                return response(200, TOKEN_PAYLOAD)
            # Always a full page of decoys and never the wanted part.
            return response(200, self._page([f"DECOY-{i}" for i in range(50)]))

        provider = make_provider()
        session_of(provider).post.side_effect = dispatch
        with pytest.raises(PartNotFoundError):
            provider.fetch_component_data(MPN)
        assert session_of(provider).post.call_count == 1 + _MAX_PAGES

    def test_offset_advances_by_the_page_size(self) -> None:
        """Each request asks for the next window, not the same one again."""
        offsets: list[int] = []

        def dispatch(url: str, **kwargs: Any) -> Any:
            if url == TOKEN_URL:
                return response(200, TOKEN_PAYLOAD)
            offsets.append(kwargs["json"]["Offset"])
            return response(200, self._page([f"DECOY-{i}" for i in range(50)]))

        provider = make_provider()
        session_of(provider).post.side_effect = dispatch
        with pytest.raises(PartNotFoundError):
            provider.fetch_component_data(MPN)
        assert offsets == [0, 50, 100, 150]


class TestRequestShape(CapturedPayloads):
    """Verify the search request matches the published OpenAPI document."""

    def test_search_is_a_post_with_a_json_body(self) -> None:
        """The endpoint is ``POST /products/v4/search/keyword``, not a GET.

        The previous path answered 404 for every request, so every part in the
        library was reported as missing.
        """
        provider = self._authenticated(response(200, search_payload(MPN)))
        provider.fetch_component_data(MPN)
        session = session_of(provider)
        search_calls = [
            call for call in session.method_calls if call.args and call.args[0] == PRODUCT_URL
        ]
        assert search_calls, "the search endpoint was never called"
        assert search_calls[0].kwargs["json"]["Keywords"] == MPN
        assert session.get.call_count == 0, "the search must not be a GET"

    def test_search_headers(self) -> None:
        """The bearer token and client id are both sent."""
        provider = self._authenticated(response(200, search_payload(MPN)))
        provider.fetch_component_data(MPN)
        headers = [
            call.kwargs["headers"]
            for call in session_of(provider).method_calls
            if call.args and call.args[0] == PRODUCT_URL
        ][0]
        assert headers["Authorization"] == "Bearer tok-123"
        assert headers["X-DIGIKEY-Client-Id"] == "client"


class TestNotFound(CapturedPayloads):
    """Verify genuine misses are distinguished from API failures."""

    def test_unstocked_part_raises_not_found(self) -> None:
        """DigiKey returns 200 with no products for an unstocked part number."""
        provider = self._authenticated(response(200, search_payload("RC0402JR-7D0RL")))
        with pytest.raises(PartNotFoundError):
            provider.fetch_component_data("RC0402JR-7D0RL")

    def test_fuzzy_only_result_raises_not_found(self) -> None:
        """A part number with no exact match is a miss, not a wrong answer.

        ``RV-3028-C7`` matches four development boards but no RTC, so accepting a
        fuzzy result would write a completely different component into the
        library.
        """
        payload = search_payload("RV-3028-C7")
        payload["Products"][0]["ManufacturerProductNumber"] = "SOMETHING-ELSE"
        provider = self._authenticated(response(200, payload))
        with pytest.raises(PartNotFoundError):
            provider.fetch_component_data("RV-3028-C7")

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

    def test_error_body_is_included_in_the_message(self) -> None:
        """A failure says what the API actually replied."""
        provider = self._authenticated(response(403, None))
        with pytest.raises(ProviderAPIError, match="error body"):
            provider.fetch_component_data(MPN)

    def test_malformed_json_raises(self) -> None:
        """A non-JSON body is reported as a provider error."""
        provider = self._authenticated(response(200, None))
        with pytest.raises(ProviderAPIError, match="Malformed"):
            provider.fetch_component_data(MPN)

    def test_transport_error_during_search(self) -> None:
        """A connection failure during search is wrapped, not reported as a miss."""
        provider = make_provider()

        def dispatch(url: str, **_: Any) -> Any:
            if url == TOKEN_URL:
                return response(200, TOKEN_PAYLOAD)
            raise requests.Timeout("slow")

        session_of(provider).post.side_effect = dispatch
        with pytest.raises(ProviderAPIError, match="search failed"):
            provider.fetch_component_data(MPN)


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
        provider = make_provider()

        def dispatch(url: str, **_: Any) -> Any:
            if url == TOKEN_URL:
                return response(200, TOKEN_PAYLOAD)
            return response(200, search_payload(MPN))

        session_of(provider).post.side_effect = dispatch
        provider.fetch_component_data(MPN)
        provider.fetch_component_data(MPN)
        assert session_of(provider).get.call_count == 0
        assert len([c for c in session_of(provider).method_calls
                    if c.args and c.args[0] == TOKEN_URL]) == 1

