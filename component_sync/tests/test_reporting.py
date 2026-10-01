"""Tests for field selection and the grouped review report."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from component_sync.models import ChangeAction, ProcessResult, PropertyChange
from component_sync.processors.kicad_processor import KiCadSymProcessor
from component_sync.reporting import Palette, default_palette, render_report
from component_sync.selection import FieldSelection, split_field_list


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
        assert "+ PART-1: Digikey = 'https://www.digikey.com/x'" in out

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
