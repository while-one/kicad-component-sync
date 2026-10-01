"""Tests for field selection and the grouped review report."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from component_sync.models import ChangeAction, ProcessResult, PropertyChange
from component_sync.processors.kicad_processor import KiCadSymProcessor
from component_sync.reporting import (
    Palette,
    _truncate,
    default_palette,
    render_report,
)
from component_sync.selection import FieldSelection, split_field_list

from .conftest import StubProvider


def change(
    field: str,
    old: str | None,
    new: str,
    *,
    identifier: str = "PART-1",
    action: ChangeAction | None = None,
) -> PropertyChange:
    """Build a property change for a test.

    Args:
        field: Target field name.
        old: Existing value, or ``None`` for an addition.
        new: Desired value.
        identifier: Part or symbol the change applies to.
        action: Explicit action, inferred from ``old`` when omitted.

    Returns:
        The constructed change.
    """
    if action is None:
        action = ChangeAction.ADD if old is None else ChangeAction.UPDATE
    return PropertyChange(identifier, field, old, new, action)


class TestSplitFieldList:
    """Verify parsing of the comma separated argument."""

    def test_simple_list(self) -> None:
        """Names are split and case-folded."""
        assert split_field_list("Value,Package") == frozenset({"value", "package"})

    def test_whitespace_and_trailing_comma(self) -> None:
        """Stray spacing and a trailing comma are harmless."""
        assert split_field_list(" Value , Package ,") == frozenset({"value", "package"})

    def test_empty_string_yields_nothing(self) -> None:
        """An empty argument selects nothing rather than everything."""
        assert split_field_list("") == frozenset()


class TestAdmits:
    """Verify which fields a run may touch."""

    def test_no_filter_admits_everything(self) -> None:
        """The default is unrestricted."""
        selection = FieldSelection()
        assert selection.admits("Value")
        assert selection.admits("Description")

    def test_skip_excludes_a_field(self) -> None:
        """``--skip`` withholds the named field."""
        selection = FieldSelection.build(skip="Description")
        assert not selection.admits("Description")
        assert selection.admits("Value")

    def test_only_restricts_to_the_named_fields(self) -> None:
        """``--only`` admits nothing else."""
        selection = FieldSelection.build(only="Value")
        assert selection.admits("Value")
        assert not selection.admits("Description")

    def test_only_wins_over_skip(self) -> None:
        """Combining both is not contradictory: ``only`` takes precedence."""
        selection = FieldSelection.build(only="Value", skip="Package")
        assert selection.admits("Value")

    def test_matching_is_case_insensitive(self) -> None:
        """The user types ``--only value``; the library stores ``Value``."""
        assert FieldSelection.build(only="value").admits("Value")


class TestIgnoreCase:
    """Verify suppression of case-only differences."""

    def test_case_only_difference_is_cosmetic(self) -> None:
        """``Yageo`` becoming ``YAGEO`` is never worth a review decision."""
        assert FieldSelection(ignore_case=True).is_cosmetic("Yageo", "YAGEO")

    def test_surrounding_whitespace_is_also_absorbed(self) -> None:
        """Padding is not a meaningful change either."""
        assert FieldSelection(ignore_case=True).is_cosmetic(" 0402 ", "0402")

    def test_a_real_difference_is_not_cosmetic(self) -> None:
        """``Murata`` becoming ``Murata Electronics`` is a real change."""
        assert not FieldSelection(ignore_case=True).is_cosmetic("Murata", "Murata Electronics")

    def test_adding_a_field_is_never_cosmetic(self) -> None:
        """There is no previous value to differ from."""
        assert not FieldSelection(ignore_case=True).is_cosmetic(None, "X")

    def test_disabled_by_default(self) -> None:
        """Without the flag, case differences are reported as changes."""
        assert not FieldSelection().is_cosmetic("Yageo", "YAGEO")


class TestSuppressionDisclosure:
    """A filtered run must say what it withheld."""

    def test_withheld_fields_are_recorded(self) -> None:
        """Excluded changes are remembered for the report."""
        selection = FieldSelection.build(skip="Description")
        selection.filter_changes((change("Description", "a", "b"), change("Value", "x", "y")))
        assert selection.suppressed_fields() == ("Description",)

    def test_nothing_withheld_reports_nothing(self) -> None:
        """An unrestricted run has nothing to disclose."""
        selection = FieldSelection()
        selection.filter_changes((change("Value", "x", "y"),))
        assert selection.suppressed_fields() == ()

    def test_two_selections_compare_equal(self) -> None:
        """Suppression state does not affect equality of configurations."""
        a = FieldSelection.build(skip="Description")
        b = FieldSelection.build(skip="Description")
        a.filter_changes((change("Description", "x", "y"),))
        assert a == b

    def test_describe_mentions_each_active_filter(self) -> None:
        """The footer explains the configuration in force."""
        text = FieldSelection.build(only="Value", ignore_case=True).describe()
        assert "only" in text
        assert "ignore-case" in text
        assert FieldSelection().describe() == ""


class TestReportGrouping:
    """Verify the report separates changes by how much attention they need."""

    @staticmethod
    def _result() -> ProcessResult:
        """Return a result covering every risk class.

        Returns:
            A synthetic result with overwrites, adds, values and a miss.
        """
        return ProcessResult(
            file_path="lib.kicad_sym",
            changes=(
                change("Description", "old prose", "vendor prose"),
                change("Package", "0402", "0402 (1005 Metric)"),
                change("Value", "10K", "10 kOhm"),
                change("Digikey", None, "https://www.digikey.com/x"),
                change("Voltage Rating", None, "50V"),
            ),
            missing_parts=("GONE-1",),
            dry_run=True,
            lookups="cache: 0 reused, 1 requested",
        )

    @staticmethod
    def _plain(**kwargs: object) -> str:
        """Render the report without colour.

        Args:
            **kwargs: Overrides passed to :func:`render_report`.

        Returns:
            The rendered report.
        """
        options: dict[str, object] = {"palette": Palette(False), "selection": FieldSelection()}
        options.update(kwargs)
        return render_report(TestReportGrouping._result(), **options)  # type: ignore[arg-type]

    def test_counts_come_first(self) -> None:
        """The four risk classes are summarised before any detail."""
        out = self._plain()
        for label in ("needs review", "value edits", "safe adds", "unresolved"):
            assert label in out
        assert out.index("needs review") < out.index("NEEDS REVIEW")

    def test_counts_are_correct(self) -> None:
        """Two overwrites, one value, two adds, one unresolved."""
        out = self._plain()
        assert "needs review     2" in out
        assert "value edits      1" in out
        assert "safe adds        2" in out
        assert "unresolved       1" in out

    def test_value_changes_get_their_own_section(self) -> None:
        """The deliberate convention change is called out separately."""
        out = self._plain()
        assert "VALUE CONVENTION  (1)" in out
        assert "'10K' -> '10 kOhm'" in out

    def test_additions_are_labelled_as_additions(self) -> None:
        """An add is visually distinct from an overwrite."""
        out = self._plain()
        assert "  + PART-1" in out
        assert "Digikey = 'https://www.digikey.com/x'" in out

    def test_changes_are_grouped_under_one_identifier(self) -> None:
        """A part needing several fields is printed as a single block."""
        out = self._plain()
        lines = out.splitlines()
        header = next(i for i, line in enumerate(lines) if line == "  ~ PART-1")
        # Two overwrites plus one value, but the header is printed once.
        assert lines[header + 1].startswith("        Description ")
        assert lines[header + 2].startswith("        Package ")
        assert not any(
            line.startswith("  ~ PART-1") for line in lines[header + 1 : header + 4]
        )

    def test_group_header_is_not_repeated_per_field(self) -> None:
        """The identifier appears once per section, fields nest beneath it."""
        out = self._plain()
        lines = out.splitlines()
        headers = [line for line in lines if line.startswith("  ~ PART-1")]
        fields = [line for line in lines if line.startswith("        ")]
        # One header per section the part appears in: NEEDS REVIEW and
        # VALUE CONVENTION, and its two adds under SAFE ADDS.
        assert len(headers) == 2
        assert len(fields) == 5

    def test_field_lines_are_indented_further(self) -> None:
        """The nesting makes the grouping readable at a glance."""
        assert "        Description " in self._plain()

    def test_a_part_may_appear_in_several_sections(self) -> None:
        """Sections stay independent, so a part is listed once in each."""
        result = ProcessResult(
            file_path="lib.kicad_sym",
            changes=(
                change("Description", "old", "new", identifier="P1"),
                change("Value", "10K", "10 kOhm", identifier="P1"),
                change("Digikey", None, "https://x", identifier="P1"),
            ),
        )
        out = render_report(result, palette=Palette(False), selection=FieldSelection())
        assert out.count("  ~ P1") == 2, "once in NEEDS REVIEW, once in VALUE CONVENTION"
        assert out.count("  + P1") == 1, "once in SAFE ADDS"

    def test_groups_keep_first_appearance_order(self) -> None:
        """Parts are listed in the order they were first proposed."""
        result = ProcessResult(
            file_path="lib.kicad_sym",
            changes=(
                change("Description", "a", "b", identifier="ZULU"),
                change("Package", "c", "d", identifier="ALPHA"),
                change("Datasheet", "", "e", identifier="ZULU"),
            ),
        )
        out = render_report(result, palette=Palette(False), selection=FieldSelection())
        assert out.index("ZULU") < out.index("ALPHA")

    def test_a_part_with_only_additions_gets_an_add_marker(self) -> None:
        """The header marker reflects the group's action, not a per-line one."""
        result = ProcessResult(
            file_path="lib.kicad_sym",
            changes=(
                change("Digikey", None, "https://x", identifier="P1"),
                change("Package", None, "0402", identifier="P1"),
            ),
        )
        out = render_report(result, palette=Palette(False), selection=FieldSelection())
        assert "  + P1" in out
        assert "  ~ P1" not in out


    def test_unresolved_explains_itself(self) -> None:
        """The miss list tells the author what to do about it."""
        out = self._plain()
        assert "UNRESOLVED  (1)" in out
        assert "! GONE-1" in out
        assert "not stocked" in out

    def test_nothing_to_do_is_stated(self) -> None:
        """An empty run does not print empty sections."""
        out = render_report(
            ProcessResult(file_path="lib.kicad_sym"),
            palette=Palette(False),
            selection=FieldSelection(),
        )
        assert "already up to date" in out
        assert "NEEDS REVIEW" not in out

    def test_field_breakdown_ranks_by_count(self) -> None:
        """The per-field tally is ordered so the dominant field leads."""
        out = self._plain()
        assert "Description 1" in out and "Package 1" in out

    def test_long_values_are_truncated(self) -> None:
        """A 250 character URL must not destroy the layout."""
        out = self._plain(width=20)
        assert "…" in out
        longest = max(len(line) for line in out.splitlines())
        assert longest < 140, longest

    def test_filters_are_disclosed_in_the_footer(self) -> None:
        """The report states the configuration it ran under."""
        selection = FieldSelection.build(skip="Description")
        out = self._plain(selection=selection)
        assert "filters" in out
        assert "skip" in out


class TestMiddleElision:
    """Long values must still show what changed.

    Two DigiKey URLs for the same part share a long prefix and differ only in the
    trailing product identifier. Eliding the tail rendered both sides as the same
    string, so the change was invisible in the report.
    """

    def test_url_change_is_visible(self) -> None:
        """The differing tail survives abbreviation."""
        old = "https://www.digikey.com/en/products/detail/murata-electronics/GCM155/12345"
        new = "https://www.digikey.com/en/products/detail/murata-electronics/GCM155/99999"
        result = ProcessResult(
            file_path="lib",
            changes=(change("Digikey", old, new, identifier="GCM155"),),
        )
        out = render_report(result, palette=Palette(False), selection=FieldSelection())
        assert "12345" in out
        assert "99999" in out

    def test_head_and_tail_are_kept(self) -> None:
        """The host and the product identifier both survive."""
        text = "https://www.digikey.com/en/products/detail/x/" + "y" * 200
        short = _truncate(text, 40)
        assert len(short) <= 40
        assert short.startswith("https://www.digikey")
        assert "…" in short
        assert short.endswith("y")

    def test_short_values_are_untouched(self) -> None:
        """Only overlong values are abbreviated."""
        assert _truncate("50V", 60) == "50V"

    def test_exact_boundary_is_untouched(self) -> None:
        """A value exactly at the limit is not abbreviated."""
        text = "x" * 40
        assert _truncate(text, 40) == text

    def test_narrow_width_does_not_crash(self) -> None:
        """A tiny width degrades to returning the text rather than misbehaving."""
        assert _truncate("abcdefgh", 3) == "abcdefgh"

class TestColour:
    """Verify colour is emitted only where it is wanted."""

    def test_disabled_palette_emits_no_escapes(self) -> None:
        """Plain text has no escape sequences at all."""
        out = render_report(
            TestReportGrouping._result(), palette=Palette(False), selection=FieldSelection()
        )
        assert "\033" not in out

    def test_enabled_palette_wraps_sections(self) -> None:
        """Each section is distinguishable when colour is available."""
        out = render_report(
            TestReportGrouping._result(), palette=Palette(True), selection=FieldSelection()
        )
        assert "\033[" in out
        assert Palette.OVERWRITE in out
        assert Palette.ADD in out

    def test_non_tty_defaults_to_plain(self) -> None:
        """Redirecting to a file must not embed escape codes."""
        assert default_palette(io.StringIO()).enabled is False

    def test_tty_defaults_to_colour(self) -> None:
        """A terminal is assumed to render colour."""
        assert default_palette(_FakeTty()).enabled is True

    def test_force_overrides_detection(self) -> None:
        """``--no-color`` wins over terminal detection."""
        assert default_palette(_FakeTty(), force=False).enabled is False
        assert default_palette(io.StringIO(), force=True).enabled is True

    def test_no_color_environment_variable_is_honoured(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The ``NO_COLOR`` convention is respected."""
        monkeypatch.setenv("NO_COLOR", "1")
        assert default_palette(_FakeTty()).enabled is False


class _FakeTty(io.StringIO):
    """A stream that claims to be a terminal."""

    def isatty(self) -> bool:
        """Report terminal status.

        Returns:
            Always True.
        """
        return True


class TestProcessorIntegration:
    """Verify filtering is applied to the file, not just the report."""

    def test_only_value_writes_nothing_else(
        self, tmp_path: Path, kicad_sym_text: str, stub_provider: object
    ) -> None:
        """A restricted run leaves every other field untouched on disk."""
        from component_sync.models import ComponentData

        provider = stub_provider
        provider.catalogue["GRM155R61C104KA88D"] = ComponentData(  # type: ignore[attr-defined]
            mpn="GRM155R61C104KA88D",
            manufacturer="Should Not Appear",
            value="100 nF",
            package="0402",
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        processor = KiCadSymProcessor(
            provider,  # type: ignore[arg-type]
            fields=FieldSelection.build(only="Value"),
            colour=False,
        )
        result = processor.process(path)

        assert [c.field_name for c in result.changes] == ["Value"]
        written = path.read_text(encoding="utf-8")
        assert "100 nF" in written
        assert "Should Not Appear" not in written

    def test_ignore_case_prevents_the_write_entirely(
        self, tmp_path: Path, kicad_sym_text: str, stub_provider: object
    ) -> None:
        """A cosmetic change is not merely hidden, it is not written.

        The library fixture already carries ``Voltage Rating = "0 VDC"``, so a
        vendor value of ``"0 vdc"`` differs from it only in case. Without the
        filter the file would be rewritten for no reason the author would care
        about.
        """
        from component_sync.models import ComponentData

        provider = stub_provider
        provider.catalogue["GRM155R61C104KA88D"] = ComponentData(  # type: ignore[attr-defined]
            mpn="GRM155R61C104KA88D", voltage_text="0 vdc"
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        before = path.read_text(encoding="utf-8")
        processor = KiCadSymProcessor(
            provider,  # type: ignore[arg-type]
            fields=FieldSelection.build(ignore_case=True),
            colour=False,
        )
        result = processor.process(path)

        assert result.changes == ()
        assert path.read_text(encoding="utf-8") == before
        assert processor.fields.suppressed_fields() == ("Voltage Rating",)

    def test_same_change_is_written_without_the_filter(
        self, tmp_path: Path, kicad_sym_text: str, stub_provider: object
    ) -> None:
        """The filter is what suppresses it, not the provider."""
        from component_sync.models import ComponentData

        provider = stub_provider
        provider.catalogue["GRM155R61C104KA88D"] = ComponentData(  # type: ignore[attr-defined]
            mpn="GRM155R61C104KA88D", voltage_text="0 vdc"
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        result = KiCadSymProcessor(provider, colour=False).process(path)  # type: ignore[arg-type]

        assert [c.field_name for c in result.changes] == ["Voltage Rating"]
        assert 'property "Voltage Rating" "0 vdc"' in path.read_text(encoding="utf-8")


class TestFailedLookupsAreReported:
    """A provider fault on one part must not discard the whole run.

    Aborting used to throw away the other 56 results, so an exhausted rate limit
    on the last part produced no report at all.
    """

    def test_not_queried_is_a_section_of_its_own(self) -> None:
        """A failed lookup is visibly different from an unstocked part."""
        result = ProcessResult(
            file_path="lib",
            failed_parts=(("P1", "DigiKey daily request quota exhausted"),),
        )
        out = render_report(result, palette=Palette(False), selection=FieldSelection())
        assert "NOT QUERIED  (1)" in out
        assert "not queried" in out
        assert "re-running may well succeed" in out
        assert "P1" in out
        assert "quota exhausted" in out

    def test_failed_and_missing_are_separate(self) -> None:
        """Conflating them would hide a transient fault as a data problem."""
        result = ProcessResult(
            file_path="lib",
            missing_parts=("GONE-1",),
            failed_parts=(("P1", "rate limited"),),
        )
        out = render_report(result, palette=Palette(False), selection=FieldSelection())
        assert "UNRESOLVED  (1)" in out
        assert "NOT QUERIED  (1)" in out
        assert "! GONE-1" in out
        assert "x P1" in out

    def test_incomplete_covers_both_kinds(self) -> None:
        """A write run must exit non-zero for either."""
        assert not ProcessResult(file_path="x").incomplete
        assert ProcessResult(file_path="x", missing_parts=("a",)).incomplete
        assert ProcessResult(file_path="x", failed_parts=(("a", "b"),)).incomplete


class TestPartialRunSurvives:
    """A failing part mid-run still leaves the rest reported."""

    def test_run_continues_past_a_provider_fault(self, tmp_path: Path) -> None:
        """The healthy part is still enriched after the failing one.

        The library holds two symbols. The first part raises a rate limit, the
        second resolves normally, and both outcomes must reach the report.
        """
        from component_sync.exceptions import RateLimitError
        from component_sync.models import ComponentData

        def block(name: str, part: str) -> str:
            """Return one symbol block carrying a Part property.

            Args:
                name: Symbol name.
                part: Manufacturer part number.

            Returns:
                The symbol source.
            """
            return (
                f'\t(symbol "{name}"\n'
                f'\t\t(property "Part" "{part}"\n'
                f"\t\t\t(at 0 0 0)\n"
                f"\t\t)\n"
                f"\t)\n"
            )

        class Flaky(StubProvider):
            def fetch_component_data(self, mpn: str) -> ComponentData:
                if mpn == "BAD-1":
                    raise RateLimitError("per-minute limit", daily=False)
                return ComponentData(mpn=mpn, manufacturer="YAGEO", value="10 kOhm")

        library = (
            "(kicad_symbol_lib\n"
            "\t(version 20231120)\n"
            + block("BROKEN", "BAD-1")
            + block("FINE", "GOOD-1")
            + ")\n"
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(library, encoding="utf-8")

        result = KiCadSymProcessor(Flaky({}), dry_run=True, colour=False).process(path)

        assert [mpn for mpn, _ in result.failed_parts] == ["BAD-1"]
        assert "per-minute limit" in result.failed_parts[0][1]
        assert {c.identifier for c in result.changes} == {"FINE"}
        assert "BROKEN" not in {c.identifier for c in result.changes}
        assert result.incomplete

    def test_daily_quota_stops_the_run(self, tmp_path: Path) -> None:
        """Once the daily allowance is gone, every later request will fail too.

        Continuing would print the same refusal once per remaining part and bury
        the one fact the author needs, so the run stops immediately.
        """
        from component_sync.exceptions import RateLimitError
        from component_sync.models import ComponentData

        asked: list[str] = []

        class Exhausted(StubProvider):
            def fetch_component_data(self, mpn: str) -> ComponentData:
                asked.append(mpn)
                raise RateLimitError("daily quota exhausted", daily=True)

        library = (
            "(kicad_symbol_lib\n"
            "\t(version 20231120)\n"
            + '    (symbol "A"\n\t\t(property "Part" "P-1"\n\t\t\t(at 0 0 0)\n\t\t)\n\t)\n'
            + '    (symbol "B"\n\t\t(property "Part" "P-2"\n\t\t\t(at 0 0 0)\n\t\t)\n\t)\n'
            + ")\n"
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(library, encoding="utf-8")

        with pytest.raises(RateLimitError):
            KiCadSymProcessor(Exhausted({}), dry_run=True, colour=False).process(path)

        assert asked == ["P-1"], "stopped after the first refusal"
