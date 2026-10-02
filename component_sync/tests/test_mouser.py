"""Tests for the Mouser sourcing provider.

Mouser is a **sourcing** provider: its Search API returns no parametric data at
all, only ``Packaging`` and ``Standard Pack Qty`` in ``ProductAttributes``. These
tests therefore assert that it contributes a link and nothing else, and that it
refuses to guess when a part number is ambiguous.

The payloads in ``fixtures/mouser_search.json`` were **captured from the live
API**, not invented, for the same reason as the DigiKey fixtures: a fabricated
fixture only proves the code agrees with its author.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast
from unittest import mock

import pytest
import requests

from component_sync.exceptions import (
    AmbiguousPartError,
    ConfigurationError,
    PartNotFoundError,
    ProviderAPIError,
    RateLimitError,
)
from component_sync.providers.base import ProviderRole
from component_sync.providers.mouser import (
    LINK_FIELD,
    MouserProvider,
    _is_rate_limit,
    _normalise_manufacturer,
    _same_manufacturer,
)

MPN = "RC0402FR-0710KL"

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "mouser_search.json"
CAPTURED: dict[str, Any] = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def payload(mpn: str) -> dict[str, Any]:
    """Return the captured response for a part number.

    Args:
        mpn: A part number present in the fixture.

    Returns:
        A response body shaped as the API returned it.

    Raises:
        KeyError: If the part number was never captured.
    """
    entry = CAPTURED[mpn]
    return {
        "Errors": entry.get("_errors") or [],
        "SearchResults": {
            "NumberOfResult": entry.get("_results"),
            "Parts": entry.get("_parts") or [],
        },
    }


def response(status: int = 200, body: Any = None) -> mock.MagicMock:
    """Build a mock HTTP response.

    Args:
        status: HTTP status code.
        body: JSON payload, or ``None`` to make ``.json()`` raise.

    Returns:
        A mock response object.
    """
    resp = mock.MagicMock()
    resp.status_code = status
    resp.text = "error body"
    resp.headers = {}
    if body is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = body
    return resp


def make_provider(**kwargs: Any) -> MouserProvider:
    """Build a provider with a stub key and a mocked session.

    Args:
        **kwargs: Overrides passed to the constructor.

    Returns:
        A configured provider.
    """
    session = mock.MagicMock(spec=requests.Session)
    options: dict[str, Any] = {"api_key": "test-key", "session": session}
    options.update(kwargs)
    return MouserProvider(**options)


def session_of(provider: MouserProvider) -> mock.MagicMock:
    """Return the provider's session as a mock.

    Args:
        provider: The provider under test.

    Returns:
        The underlying mock session.
    """
    return cast(mock.MagicMock, provider._session)


class TestRole:
    """Verify Mouser is registered as a sourcing provider, not a data one."""

    def test_role_is_source(self) -> None:
        """It must never be offered as the primary data provider."""
        assert MouserProvider.role is ProviderRole.SOURCE

    def test_not_a_data_provider(self) -> None:
        """The two roles are not interchangeable."""
        assert MouserProvider.role is not ProviderRole.DATA


class TestAuthentication:
    """Verify credential handling."""

    def test_missing_key_raises(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """An absent key is a configuration error, not a silent no-op."""
        monkeypatch.delenv("MOUSER_API_KEY", raising=False)
        provider = MouserProvider(session=mock.MagicMock())
        with pytest.raises(ConfigurationError, match="Mouser API key is missing"):
            provider.authenticate()

    def test_key_read_from_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The environment is how the sourcing pass enables itself."""
        monkeypatch.setenv("MOUSER_API_KEY", "from-env")
        assert MouserProvider().api_key == "from-env"


class TestLinkOnlyContribution:
    """Mouser may contribute a link and nothing else."""

    def test_returns_the_product_link(self) -> None:
        """The URL is used exactly as the API supplied it."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        links = provider.fetch_source_links(MPN)
        assert list(links) == [LINK_FIELD]
        assert links[LINK_FIELD].startswith("https://www.mouser.")

    def test_url_is_never_rewritten(self) -> None:
        """The locale and ``?qs=`` parameter are left as they arrived.

        Rewriting them would mean constructing a URL, and a fabricated URL is
        indistinguishable from a real one in a symbol library.
        """
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        returned = CAPTURED[MPN]["_parts"][0]["ProductDetailUrl"]
        assert provider.fetch_source_links(MPN)[LINK_FIELD] == returned

    def test_contributes_no_component_data(self) -> None:
        """No Value, voltage, temperature or package may come from Mouser."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        data = provider.fetch_component_data(MPN)
        properties = data.as_properties()
        for forbidden in ("Value", "Manufacturer", "Package", "Description",
                          "Datasheet", "Voltage Min", "Temperature Min"):
            assert forbidden not in properties, forbidden
        assert data.value == ""
        assert data.temp_min == "" and data.temp_max == ""

    def test_links_are_separate_from_data_properties(self) -> None:
        """A link can never be mistaken for a value."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        data = provider.fetch_component_data(MPN)
        assert LINK_FIELD in data.source_properties()
        assert LINK_FIELD not in data.as_properties()


class TestRequestShape:
    """The key is a query parameter and the part number goes in the body."""

    def test_api_key_is_in_the_query_string(self) -> None:
        """Putting it in a header, as one would for DigiKey, does not work."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        provider.fetch_source_links(MPN)
        url = session_of(provider).post.call_args[0][0]
        assert "apiKey=test-key" in url

    def test_exact_match_is_requested(self) -> None:
        """Fuzzy results would attach the wrong product."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        provider.fetch_source_links(MPN)
        body = session_of(provider).post.call_args.kwargs["json"]
        assert body["SearchByPartRequest"]["partSearchOptions"] == "Exact"
        assert body["SearchByPartRequest"]["mouserPartNumber"] == MPN

    def test_it_is_a_post(self) -> None:
        """The endpoint only accepts POST."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        provider.fetch_source_links(MPN)
        assert session_of(provider).post.call_count == 1
        assert session_of(provider).get.call_count == 0


class TestAmbiguousPartNumbers:
    """A bare part number is not unique across manufacturers.

    ``1028`` returns six unrelated products, all numbered exactly ``1028``.
    Taking the first result attaches a link to side cutting pliers to a battery
    holder symbol.
    """

    def test_ambiguity_is_reported_not_guessed(self) -> None:
        """With no manufacturer to disambiguate, no link is offered."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload("1028"))
        with pytest.raises(AmbiguousPartError) as info:
            provider.fetch_source_links("1028")
        message = info.value.message
        assert "6 unrelated products" in message
        assert "Keystone Electronics" in message, "the true part is listed"

    def test_manufacturer_disambiguates(self) -> None:
        """The library's Manufacturer selects the right product."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload("1028"))
        link = provider.fetch_source_links("1028", "Keystone Electronics")[LINK_FIELD]
        assert "keystone-electronics" in link.casefold()
        assert "sargent" not in link.casefold(), "must not be the pliers"

    def test_a_wrong_hint_still_selects_something(self) -> None:
        """The hint is genuinely used, which is what makes case 2 meaningful."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload("1028"))
        link = provider.fetch_source_links("1028", "Sargent Tools")[LINK_FIELD]
        assert "sargent-tools" in link.casefold()

    def test_unmatched_hint_stays_ambiguous(self) -> None:
        """A manufacturer that matches nothing must not fall back to the first."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload("1028"))
        with pytest.raises(AmbiguousPartError):
            provider.fetch_source_links("1028", "Vishay")

    def test_unambiguous_part_needs_no_hint(self) -> None:
        """A unique number resolves with no manufacturer at all."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        assert provider.fetch_source_links(MPN, "")[LINK_FIELD]

    def test_candidates_are_described(self) -> None:
        """The error names each product so the author can recognise one."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload("1028"))
        with pytest.raises(AmbiguousPartError) as info:
            provider.fetch_source_links("1028")
        joined = " ".join(info.value.candidates)
        assert "Sargent Tools" in joined
        assert "Heyco" in joined
        assert "MakerBot" in joined


class TestManufacturerComparison:
    """Distributors spell the same manufacturer differently."""

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Murata", "Murata Electronics"),
            ("YAGEO", "Yageo"),
            ("Texas Instruments", "Texas Instruments Inc"),
            ("NXP USA Inc.", "NXP USA Inc"),
        ],
    )
    def test_equivalent_spellings(self, left: str, right: str) -> None:
        """The hint must survive normal distributor naming differences."""
        assert _same_manufacturer(left, right)

    @pytest.mark.parametrize(
        ("left", "right"),
        [
            ("Keystone Electronics", "Sargent Tools"),
            ("Murata", "TDK"),
            ("YAGEO", "Vishay"),
            ("Adafruit", "MakerBot"),
        ],
    )
    def test_genuinely_different(self, left: str, right: str) -> None:
        """Different companies must not be conflated."""
        assert not _same_manufacturer(left, right)

    def test_division_is_treated_as_the_parent(self) -> None:
        """A product line is the same manufacturer for link purposes.

        ``TE Connectivity Linx`` is a TE division, and Mouser lists the
        manufacturer of a ``BAT-HLD-001`` that way. Refusing to match would
        leave the part unresolved for a naming convention rather than a real
        difference.
        """
        assert _same_manufacturer("TE Connectivity", "TE Connectivity Linx")

    def test_known_limitation_very_short_names(self) -> None:
        """An initialism is not recoverable from its spelled-out form.

        ``C&K`` normalises to ``ck`` and ``C and K`` to ``candk``; no
        reasonable string comparison relates them. The part stays reported as
        ambiguous rather than being attached to the wrong product, which is the
        safe direction to fail in.
        """
        assert not _same_manufacturer("C&K", "C and K")

    def test_empty_never_matches(self) -> None:
        """An absent manufacturer cannot disambiguate anything."""
        assert not _same_manufacturer("", "Murata")
        assert not _same_manufacturer("Murata", "")

    def test_normalisation_strips_punctuation(self) -> None:
        """Comparison is on letters and digits only."""
        assert _normalise_manufacturer("C&K Switches") == "ckswitches"


class TestNotFoundAndErrors:
    """Verify failure modes."""

    def test_absent_part_is_not_found(self) -> None:
        """No products is an ordinary miss."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload("ZZZ-NOT-REAL-999"))
        with pytest.raises(PartNotFoundError):
            provider.fetch_source_links("ZZZ-NOT-REAL-999")

    def test_batch_drop_is_detected_by_comparison(self) -> None:
        """A silently dropped part is a miss, not a success.

        Mouser returns no error at all for a part number it cannot find; the
        miss is only visible by comparing what was asked for against what came
        back.
        """
        provider = make_provider()
        body = payload(MPN)
        body["SearchResults"]["Parts"] = []
        session_of(provider).post.return_value = response(200, body)
        with pytest.raises(PartNotFoundError):
            provider.fetch_source_links(MPN)

    def test_rejected_key_arrives_as_http_200(self) -> None:
        """Checking the status code alone would read this as success.

        Mouser answers a bad key with ``200`` and a populated ``Errors`` array.
        """
        provider = make_provider()
        body = {
            "Errors": [
                {
                    "Id": 0,
                    "Code": "Invalid",
                    "Message": "Invalid unique identifier.",
                    "PropertyName": "API Key",
                }
            ],
            "SearchResults": {"NumberOfResult": 0, "Parts": []},
        }
        session_of(provider).post.return_value = response(200, body)
        with pytest.raises(ProviderAPIError, match="Invalid unique identifier"):
            provider.fetch_source_links(MPN)

    def test_non_200_is_reported(self) -> None:
        """A genuine HTTP failure is wrapped."""
        provider = make_provider()
        session_of(provider).post.return_value = response(500, {"detail": "server error"})
        with pytest.raises(ProviderAPIError, match="HTTP 500: server error"):
            provider.fetch_source_links(MPN)

    def test_status_is_not_repeated(self) -> None:
        """The message must not read 'HTTP 500: HTTP 500'."""
        provider = make_provider()
        session_of(provider).post.return_value = response(500, {"detail": "boom"})
        with pytest.raises(ProviderAPIError) as info:
            provider.fetch_source_links(MPN)
        assert "HTTP 500: HTTP" not in info.value.message

    def test_transport_error_is_wrapped(self) -> None:
        """A connection failure is wrapped, not propagated raw."""
        provider = make_provider()
        session_of(provider).post.side_effect = requests.Timeout("slow")
        with pytest.raises(ProviderAPIError, match="Mouser search failed"):
            provider.fetch_source_links(MPN)

    def test_malformed_body_is_reported(self) -> None:
        """A non-JSON body is a provider error."""
        provider = make_provider()
        session_of(provider).post.return_value = response(200)
        with pytest.raises(ProviderAPIError, match="Malformed"):
            provider.fetch_source_links(MPN)


class TestSessionLifecycle:
    """Verify connection reuse and cleanup."""

    def test_injected_session_is_not_closed(self) -> None:
        """An injected session belongs to its caller."""
        provider = make_provider()
        provider.close()
        session_of(provider).close.assert_not_called()

    def test_owned_session_is_closed(self) -> None:
        """A session the provider created is closed with it."""
        session = mock.MagicMock(spec=requests.Session)
        provider = MouserProvider(api_key="k", session=session)
        provider._owns_session = True
        provider.close()
        session.close.assert_called_once()


class TestSequencing:
    """The sourcing pass runs after the data provider and adds only links."""

    @staticmethod
    def _library(part: str, extra: str = "") -> str:
        """Return a minimal symbol library carrying a Part property.

        Args:
            part: The manufacturer part number.
            extra: Additional property blocks, already indented.

        Returns:
            The library source.
        """
        return (
            "(kicad_symbol_lib\n"
            "\t(version 20231120)\n"
            '\t(symbol "SYM"\n'
            f'\t\t(property "Part" "{part}"\n'
            "\t\t\t(at 0 0 0)\n"
            "\t\t)\n"
            f"{extra}"
            "\t)\n"
            ")\n"
        )

    def test_link_is_added_when_the_field_is_empty(self, tmp_path: Path) -> None:
        """An empty Mouser field is filled from the API.

        "Add only" means *do not replace something the author already has*, not
        *never write the field*. An empty field is an invitation.
        """
        from component_sync.models import ComponentData
        from component_sync.processors.kicad_processor import KiCadSymProcessor
        from component_sync.providers.base import BaseProvider

        class Data(BaseProvider):
            def authenticate(self) -> None:
                """No credentials needed."""

            def fetch_component_data(self, mpn: str) -> ComponentData:
                """Return a value for the part.

                Args:
                    mpn: Ignored part number.

                Returns:
                    A fixed record.
                """
                return ComponentData(mpn=mpn, value="10 kOhm", manufacturer="YAGEO")

        class Source(MouserProvider):
            def fetch_source_links(self, mpn: str, manufacturer: str = "") -> dict[str, str]:
                return {LINK_FIELD: "https://www.mouser.example/x"}

        path = tmp_path / "lib.kicad_sym"
        path.write_text(
            self._library(
                MPN,
                '\t\t(property "Mouser" ""\n\t\t\t(at 0 0 0)\n\t\t)\n',
            ),
            encoding="utf-8",
        )

        result = KiCadSymProcessor(
            Data(), dry_run=True, colour=False, sources=(Source(api_key="k"),)
        ).process(path)

        link_changes = [c for c in result.changes if c.field_name == "Mouser"]
        assert len(link_changes) == 1
        assert link_changes[0].old_value == ""
        assert link_changes[0].new_value == "https://www.mouser.example/x"

    def test_link_is_added_when_the_field_is_absent(self, tmp_path: Path) -> None:
        """A symbol with no Mouser field at all gains a hidden one."""
        from component_sync.models import ComponentData
        from component_sync.processors.kicad_processor import KiCadSymProcessor
        from component_sync.providers.base import BaseProvider

        class Data(BaseProvider):
            def authenticate(self) -> None:
                """No credentials needed."""

            def fetch_component_data(self, mpn: str) -> ComponentData:
                """Return a value for the part.

                Args:
                    mpn: Ignored part number.

                Returns:
                    A fixed record.
                """
                return ComponentData(mpn=mpn, value="10 kOhm")

        class Source(MouserProvider):
            def fetch_source_links(self, mpn: str, manufacturer: str = "") -> dict[str, str]:
                return {LINK_FIELD: "https://www.mouser.example/x"}

        path = tmp_path / "lib.kicad_sym"
        path.write_text(self._library(MPN), encoding="utf-8")

        result = KiCadSymProcessor(
            Data(), colour=False, sources=(Source(api_key="k"),)
        ).process(path)

        link_changes = [c for c in result.changes if c.field_name == "Mouser"]
        assert len(link_changes) == 1
        assert link_changes[0].old_value is None
        written = path.read_text(encoding="utf-8")
        assert "mouser.example" in written
        block = written.split('(property "Mouser"', 1)[1].split("\n\t\t)", 1)[0]
        assert "(hide yes)" in block, "a newly added link is hidden on the schematic"

    def test_existing_reseller_link_is_never_touched(self, tmp_path: Path) -> None:
        """A curated mou.sr link must survive untouched."""
        from component_sync.models import ComponentData
        from component_sync.processors.kicad_processor import KiCadSymProcessor
        from component_sync.providers.base import BaseProvider

        class Data(BaseProvider):
            def authenticate(self) -> None:
                """No credentials needed."""

            def fetch_component_data(self, mpn: str) -> ComponentData:
                """Return a value for the part.

                Args:
                    mpn: Ignored part number.

                Returns:
                    A fixed record.
                """
                return ComponentData(mpn=mpn, value="10 kOhm")

        class Source(MouserProvider):
            def fetch_source_links(self, mpn: str, manufacturer: str = "") -> dict[str, str]:
                return {LINK_FIELD: "https://www.mouser.example/should-not-appear"}

        path = tmp_path / "lib.kicad_sym"
        path.write_text(
            self._library(
                MPN,
                '\t\t(property "Mouser" "https://mou.sr/4AGo7sW"\n'
                "\t\t\t(at 0 0 0)\n\t\t)\n",
            ),
            encoding="utf-8",
        )

        result = KiCadSymProcessor(
            Data(), dry_run=True, colour=False, sources=(Source(api_key="k"),)
        ).process(path)

        assert "Mouser" not in {c.field_name for c in result.changes}
        assert "mouser.example" not in path.read_text(encoding="utf-8")
        assert "mou.sr/4AGo7sW" in path.read_text(encoding="utf-8")

    def test_data_provider_may_still_correct_a_value(self, tmp_path: Path) -> None:
        """Add-only applies to links, not to values.

        Scoping the rule too widely would stop the tool fixing a wrong Value,
        which is the whole point of running it.
        """
        from component_sync.models import ComponentData
        from component_sync.processors.kicad_processor import KiCadSymProcessor
        from component_sync.providers.base import BaseProvider

        class Data(BaseProvider):
            def authenticate(self) -> None:
                """No credentials needed."""

            def fetch_component_data(self, mpn: str) -> ComponentData:
                """Return a corrected value.

                Args:
                    mpn: Ignored part number.

                Returns:
                    A fixed record.
                """
                return ComponentData(mpn=mpn, value="10 kOhm")

        class Source(MouserProvider):
            def fetch_source_links(self, mpn: str, manufacturer: str = "") -> dict[str, str]:
                return {}

        library = self._library(MPN).replace(
            '(property "Part" "RC0402FR-0710KL"',
            '(property "Value" "10K"\n\t\t\t(at 0 0 0)\n\t\t)\n'
            '\t\t(property "Part" "RC0402FR-0710KL"',
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(library, encoding="utf-8")

        result = KiCadSymProcessor(
            Data(), dry_run=True, colour=False, sources=(Source(api_key="k"),)
        ).process(path)

        assert "Value" in {c.field_name for c in result.changes}

    def test_ambiguous_part_does_not_break_the_run(self, tmp_path: Path) -> None:
        """An ambiguous sourcing failure is contained, not fatal."""
        from component_sync.exceptions import AmbiguousPartError
        from component_sync.models import ComponentData
        from component_sync.processors.kicad_processor import KiCadSymProcessor
        from component_sync.providers.base import BaseProvider

        class Data(BaseProvider):
            def authenticate(self) -> None:
                """No credentials needed."""

            def fetch_component_data(self, mpn: str) -> ComponentData:
                """Return a value for the part.

                Args:
                    mpn: Ignored part number.

                Returns:
                    A fixed record.
                """
                return ComponentData(mpn=mpn, value="100 nF")

        class Source(MouserProvider):
            def fetch_source_links(self, mpn: str, manufacturer: str = "") -> dict[str, str]:
                raise AmbiguousPartError("1028", ("Keystone: holder",))

        path = tmp_path / "lib.kicad_sym"
        path.write_text(self._library("1028"), encoding="utf-8")

        result = KiCadSymProcessor(
            Data(), dry_run=True, colour=False, sources=(Source(api_key="k"),)
        ).process(path)

        assert result.changes, "the data provider's work is still reported"
        assert "Mouser" not in {c.field_name for c in result.changes}


class TestRateLimit:
    """Mouser answers a rate limit with HTTP 403, not 429.

    The body says ``Code: "TooManyRequests"`` and
    ``ResourceKey: "MaxCallPerMinute"``. The documented ceiling is 30 calls per
    minute, and this provider sends one part per call, so a 57-part run exceeds
    it. Treating 403 as permanent lost the sourcing links partway through
    instead of waiting.
    """

    @staticmethod
    def _refusal() -> mock.MagicMock:
        """Return the 403 Mouser returns when the per-minute limit is exceeded.

        Returns:
            A mock response.
        """
        resp = mock.MagicMock()
        resp.status_code = 403
        resp.text = '{"Errors":[]}'
        resp.headers = {}
        resp.json.return_value = {
            "Errors": [
                {
                    "Id": 0,
                    "Code": "TooManyRequests",
                    "Message": "Maximum calls per minute exceeded.",
                    "ResourceKey": "MaxCallPerMinute",
                }
            ],
            "SearchResults": None,
        }
        return resp

    def test_403_with_a_rate_limit_body_is_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A limit worth waiting out must not be treated as permanent."""
        monkeypatch.setattr("component_sync.providers.mouser.time.sleep", lambda _s: None)
        attempts = {"n": 0}

        def search(url: str, **_: object) -> mock.MagicMock:
            attempts["n"] += 1
            if attempts["n"] == 1:
                return self._refusal()
            return response(200, payload(MPN))

        provider = make_provider()
        session_of(provider).post.side_effect = search
        assert provider.fetch_source_links(MPN)[LINK_FIELD]
        assert attempts["n"] == 2, "retried once and then succeeded"

    def test_persistent_limit_is_reported_as_a_rate_limit(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Retries are bounded, and the result names the real problem."""
        monkeypatch.setattr("component_sync.providers.mouser.time.sleep", lambda _s: None)
        provider = make_provider()
        session_of(provider).post.side_effect = lambda url, **kw: self._refusal()
        with pytest.raises(RateLimitError, match="30 calls per minute"):
            provider.fetch_source_links(MPN)

    def test_a_genuine_403_is_not_retried(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A permission failure is not a rate limit and must not be retried."""
        monkeypatch.setattr("component_sync.providers.mouser.time.sleep", lambda _s: None)
        forbidden = response(403, {"Errors": [{"Message": "Access denied"}]})
        provider = make_provider()
        session_of(provider).post.side_effect = lambda url, **kw: forbidden
        with pytest.raises(ProviderAPIError, match="Access denied"):
            provider.fetch_source_links(MPN)
        assert session_of(provider).post.call_count == 1

    def test_429_is_also_treated_as_a_rate_limit(self) -> None:
        """The conventional status is handled the same way."""
        assert _is_rate_limit(429, {})
        assert _is_rate_limit(403, {"Errors": [{"Code": "TooManyRequests"}]})
        assert _is_rate_limit(403, {"Errors": [{"ResourceKey": "MaxCallPerMinute"}]})
        assert not _is_rate_limit(403, {"Errors": [{"Message": "Access denied"}]})
        assert not _is_rate_limit(500, {})

    def test_requests_are_paced(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A run stays under 30 calls per minute without being told to."""
        waits: list[float] = []
        clock = {"t": 1000.0}

        monkeypatch.setattr(
            "component_sync.providers.mouser.time.monotonic", lambda: clock["t"]
        )
        def fake_sleep(seconds: float) -> None:
            waits.append(seconds)
            clock["t"] += seconds

        monkeypatch.setattr("component_sync.providers.mouser.time.sleep", fake_sleep)
        provider = make_provider()
        session_of(provider).post.return_value = response(200, payload(MPN))
        for _ in range(3):
            provider.fetch_source_links(MPN)
        assert waits, "no pacing between requests"
        assert all(w >= 2.0 for w in waits), "under 30 calls per minute"
