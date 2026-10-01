"""Tests for session-level caching of provider lookups.

A bill of materials names the same part many times over, so the cache is what
stands between 166 component instances and 56 distinct part numbers.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from component_sync.cache import CacheStats, LookupCache, normalise_mpn
from component_sync.exceptions import PartNotFoundError, ProviderAPIError
from component_sync.models import ComponentData
from component_sync.providers.base import BaseProvider

from .conftest import StubProvider


class CountingProvider(StubProvider):
    """Stub that also records how many distinct MPNs it was asked about.

    Attributes:
        failures_before_success: Number of transport faults to raise before
            answering normally. Used to prove that faults are not cached.
    """

    name = "counting"

    def __init__(
        self,
        catalogue: dict[str, ComponentData],
        failures_before_success: int = 0,
    ) -> None:
        """Initialise the provider.

        Args:
            catalogue: Mapping of MPN to the record to return.
            failures_before_success: How many calls to fail before succeeding.
        """
        super().__init__(catalogue)
        self.failures_before_success = failures_before_success

    def fetch_component_data(self, mpn: str) -> ComponentData:
        """Return the catalogue entry, optionally failing first.

        Args:
            mpn: Part number to resolve.

        Returns:
            The matching record.

        Raises:
            PartNotFoundError: If the MPN is absent.
            ProviderAPIError: While the failure budget is non-zero.
        """
        if self.failures_before_success > 0:
            self.failures_before_success -= 1
            self.calls.append(mpn)
            raise ProviderAPIError("simulated transport fault")
        return super().fetch_component_data(mpn)


class TestNormaliseMpn:
    """Verify how part numbers are compared."""

    def test_surrounding_whitespace_is_ignored(self) -> None:
        """A hand-entered BOM often pads the part number."""
        assert normalise_mpn("  RC0402FR-0710KL  ") == normalise_mpn("RC0402FR-0710KL")

    def test_case_is_folded(self) -> None:
        """Distributors and BOMs disagree on case."""
        assert normalise_mpn("grm155r61c104ka88d") == normalise_mpn("GRM155R61C104KA88D")

    def test_internal_separators_are_significant(self) -> None:
        """NXP packing designators are part of the number, not decoration.

        Stripping the slashes would turn a real orderable part into one that
        does not exist, so only the outer whitespace is removed.
        """
        assert normalise_mpn("PCA85162T/Q900/1Y") != normalise_mpn("PCA85162T")
        assert normalise_mpn("ATS2D3G NC LFG") != normalise_mpn("ATS2D3G")


class TestCacheEffectiveness:
    """Verify that each part is requested at most once."""

    def test_repeated_part_is_requested_once(self, stub_provider: StubProvider) -> None:
        """The same MPN resolves without a second request."""
        cache = LookupCache()
        for _ in range(5):
            cache.fetch(stub_provider, "GRM155R61C104KA88D")
        assert stub_provider.calls == ["GRM155R61C104KA88D"]
        assert cache.stats.requests_made == 1
        assert cache.stats.hits == 4
        assert cache.stats.saved == 4

    def test_case_and_spacing_variants_share_an_entry(
        self, stub_provider: StubProvider
    ) -> None:
        """A BOM listing the same part three ways still costs one request."""
        cache = LookupCache()
        for variant in ("GRM155R61C104KA88D", "grm155r61c104ka88d", " GRM155R61C104KA88D "):
            cache.fetch(stub_provider, variant)
        assert stub_provider.calls == ["GRM155R61C104KA88D"]

    def test_returned_data_is_identical_on_repeat(
        self, stub_provider: StubProvider
    ) -> None:
        """A cache hit returns the very same record, not a rebuilt one."""
        cache = LookupCache()
        first = cache.fetch(stub_provider, "GRM155R61C104KA88D")
        second = cache.fetch(stub_provider, "GRM155R61C104KA88D")
        assert first is second

    def test_distinct_parts_are_each_requested(self, sample_component: ComponentData) -> None:
        """The cache keys on the part number, not just the first lookup."""
        catalogue = {
            "A-1": ComponentData(mpn="A-1", value="1 nF"),
            "B-2": ComponentData(mpn="B-2", value="10 kOhm"),
        }
        provider = StubProvider(catalogue)
        cache = LookupCache()
        for mpn in ("A-1", "B-2", "A-1", "B-2"):
            cache.fetch(provider, mpn)
        assert sorted(provider.calls) == ["A-1", "B-2"]
        assert cache.stats.requests_made == 2

    def test_providers_do_not_share_entries(self) -> None:
        """Two providers in one session must never see each other's data.

        Even two instances of the same class: they could be pointed at different
        distributor accounts and would legitimately return different prices and
        stock for the same part number.
        """
        first = StubProvider({})
        second = StubProvider({"X-1": ComponentData(mpn="X-1", value="1 nF")})
        cache = LookupCache()
        assert cache.fetch(first, "X-1") is None
        assert cache.fetch(second, "X-1") is not None
        assert first.calls == ["X-1"]
        assert second.calls == ["X-1"]


class TestMissCaching:
    """Verify that missing parts are remembered too."""

    def test_missing_part_is_requested_once(self) -> None:
        """An unstocked part is as repeated as a stocked one."""
        provider = StubProvider({})
        cache = LookupCache()
        for _ in range(4):
            assert cache.fetch(provider, "NOPE-1") is None
        assert provider.calls == ["NOPE-1"]
        assert cache.stats.negative_hits == 3
        assert cache.stats.requests_made == 1

    def test_miss_does_not_poison_a_real_part(self) -> None:
        """A later catalogue entry for the same MPN is still picked up."""
        provider = StubProvider({})
        cache = LookupCache()
        assert cache.fetch(provider, "LATE-1") is None
        provider.catalogue["LATE-1"] = ComponentData(mpn="LATE-1", value="1 nF")
        assert cache.fetch(provider, "LATE-1") is None, "a miss is remembered by design"
        cache.clear()
        assert cache.fetch(provider, "LATE-1") is not None


class TestFailureHandling:
    """Verify that transient faults are never cached."""

    def test_transient_failure_is_retried(self) -> None:
        """A transport fault must not permanently exclude a real part.

        Caching it would drop a part that is in fact available, and the symbol
        would silently never gain its fields.
        """
        provider = CountingProvider(
            {"GRM155R61C104KA88D": ComponentData(mpn="GRM155R61C104KA88D", value="100 nF")},
            failures_before_success=1,
        )
        cache = LookupCache()

        with pytest.raises(ProviderAPIError):
            cache.fetch(provider, "GRM155R61C104KA88D")
        assert cache.stats.failures == 1
        assert cache.stats.requests_made == 1

        data = cache.fetch(provider, "GRM155R61C104KA88D")
        assert data is not None
        assert data.value == "100 nF"
        assert cache.stats.failures == 1
        assert cache.stats.requests_made == 2

    def test_keyboard_interrupt_leaves_no_entry(self) -> None:
        """A cancelled run leaves nothing behind, so resuming works."""

        class Interrupting(BaseProvider):
            name = "interrupting"

            def authenticate(self) -> None:
                """No credentials needed."""

            def fetch_component_data(self, mpn: str) -> ComponentData:
                """Refuse to answer.

                Args:
                    mpn: Ignored part number.

                Raises:
                    KeyboardInterrupt: Always, as a cancellation would.
                """
                raise KeyboardInterrupt

        cache = LookupCache()
        with pytest.raises(KeyboardInterrupt):
            cache.fetch(Interrupting(), "ANY")
        with pytest.raises(KeyboardInterrupt):
            cache.fetch(Interrupting(), "ANY")
        assert cache.stats.failures == 2, "the interrupt was retried, not cached"
        assert cache.stats.requests_made == 2
        assert cache.stats.saved == 0, "nothing was served from the cache"


class TestCacheLifecycle:
    """Verify clearing and reporting."""

    def test_clear_resets_entries_and_counters(self, stub_provider: StubProvider) -> None:
        """A fresh run must not inherit the previous run's results."""
        cache = LookupCache()
        cache.fetch(stub_provider, "GRM155R61C104KA88D")
        cache.clear()
        assert cache.stats == CacheStats()
        cache.fetch(stub_provider, "GRM155R61C104KA88D")
        assert stub_provider.calls == ["GRM155R61C104KA88D", "GRM155R61C104KA88D"]

    def test_describe_reports_the_saving(self, stub_provider: StubProvider) -> None:
        """The summary makes the benefit visible to the author."""
        cache = LookupCache()
        for _ in range(3):
            cache.fetch(stub_provider, "GRM155R61C104KA88D")
        text = cache.stats.describe()
        assert "2 reused" in text
        assert "1 requested" in text

    def test_request_counts_add_up(self) -> None:
        """``requests_made`` counts only calls that left the process."""
        stats = CacheStats(hits=5, misses=3, negative_hits=2, failures=1)
        assert stats.requests_made == 4
        assert stats.saved == 7


class TestProcessorIntegration:
    """Verify the processors actually route through the cache."""

    def test_kicad_run_reports_its_saving(
        self, stub_provider: StubProvider, kicad_sym_text: str, tmp_path: Path
    ) -> None:
        """A dry run states how many requests the cache avoided."""
        from component_sync.processors.kicad_processor import KiCadSymProcessor

        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")
        processor = KiCadSymProcessor(stub_provider, dry_run=True)
        result = processor.process(path)
        assert result.lookups is not None
        assert "reused" in result.lookups

    def test_summary_omits_cache_line_when_absent(self) -> None:
        """A result with no statistics does not print an empty cache row."""
        from component_sync.models import ProcessResult

        assert "Cache" not in ProcessResult(file_path="x").summary()
        assert "Cache" in ProcessResult(file_path="x", lookups="cache: 1").summary()


class TestProviderInterfaceIsRespected:
    """Confirm the cache never bypasses the provider contract."""

    def test_any_provider_subclass_works(self) -> None:
        """The cache depends only on the abstract interface."""

        class Minimal(BaseProvider):
            name = "minimal"

            def authenticate(self) -> None:
                """No credentials needed."""

            def fetch_component_data(self, mpn: str) -> ComponentData:
                """Return a fixed record.

                Args:
                    mpn: Ignored part number.

                Returns:
                    A fixed record.
                """
                return ComponentData(mpn=mpn, value="1 nF")

        cache = LookupCache()
        assert cache.fetch(Minimal(), "ANY") is not None
        assert cache.stats.requests_made == 1


class TestPartNotFoundIsASubclassCheck:
    """Guard the fix made to a fragile type-name comparison."""

    def test_real_exception_type_is_caught(self) -> None:
        """The cache imports the real class rather than matching on its name.

        Matching ``type(exc).__name__`` would silently swallow a genuine fault
        if a provider raised a same-named exception from elsewhere.
        """
        provider = StubProvider({})
        cache = LookupCache()
        assert cache.fetch(provider, "ABSENT") is None
        assert PartNotFoundError is not None
