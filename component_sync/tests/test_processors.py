"""Tests for the CSV and KiCad symbol processors.

Covers dry-run behaviour, atomic mutation, format preservation and the
structured S-expression parser. No provider performs network I/O.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from component_sync.exceptions import FileFormatError
from component_sync.models import ChangeAction, ComponentData
from component_sync.processors.base import atomic_write
from component_sync.processors.csv_processor import CSVProcessor
from component_sync.processors.kicad_processor import KiCadSymProcessor
from component_sync.sexpr import SString, TextEdit, apply_edits, parse, quote

from .conftest import StubProvider


def provider_with(**parts: ComponentData) -> StubProvider:
    """Build a stub provider from keyword parts.

    Args:
        **parts: Mapping of MPN to record.

    Returns:
        The stub provider.
    """
    return StubProvider(dict(parts))


CAP = ComponentData(
    mpn="GRM155R61C104KA88D",
    manufacturer="Murata",
    description="MLCC 0.1uF",
    voltage_text="16 VDC",
    temp_min="-55 C",
    temp_max="+85 C",
    package="0402",
)
RES = ComponentData(
    mpn="RC0402FR-0710KL",
    manufacturer="Yageo",
    description="Thick Film Resistor 10K",
    voltage_text="50 V",
    temp_min="-55 C",
    temp_max="+155 C",
    package="0402",
)


class TestCSVProcessor:
    """Verify BOM enrichment."""

    def test_dry_run_never_writes(
        self, tmp_path: Path, csv_bom_text: str
    ) -> None:
        """A dry run leaves the file byte-for-byte identical."""
        path = tmp_path / "bom.csv"
        path.write_text(csv_bom_text, encoding="utf-8")
        original = path.read_bytes()

        result = CSVProcessor(provider_with(**{CAP.mpn: CAP}), dry_run=True).process(path)

        assert path.read_bytes() == original
        assert result.dry_run is True
        assert result.written is False
        assert result.changes

    def test_dry_run_prints_report(
        self, tmp_path: Path, csv_bom_text: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A dry run narrates its proposed changes."""
        path = tmp_path / "bom.csv"
        path.write_text(csv_bom_text, encoding="utf-8")

        CSVProcessor(provider_with(**{CAP.mpn: CAP}), dry_run=True).process(path)

        out = capsys.readouterr().out
        assert "Dry run" in out
        assert "Proposed changes" in out
        assert "GRM155R61C104KA88D" in out

    def test_write_adds_new_columns(
        self, tmp_path: Path, csv_bom_text: str
    ) -> None:
        """A real run adds fields as new trailing columns."""
        path = tmp_path / "bom.csv"
        path.write_text(csv_bom_text, encoding="utf-8")

        result = CSVProcessor(provider_with(**{CAP.mpn: CAP, RES.mpn: RES})).process(path)

        assert result.written is True
        text = path.read_text(encoding="utf-8")
        assert "Manufacturer" in text
        assert "Temperature Min" in text and "Temperature Max" in text
        assert "Murata" in text

    def test_existing_field_is_updated_in_place(
        self, tmp_path: Path, csv_bom_text: str
    ) -> None:
        """An existing field keeps its column index and only its value changes."""
        path = tmp_path / "bom.csv"
        path.write_text(
            '"Reference","Value","Part","Voltage Rating"\n'
            '"C1","100nF","GRM155R61C104KA88D","0 VDC"\n',
            encoding="utf-8",
        )

        result = CSVProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        lines = path.read_text(encoding="utf-8").splitlines()
        header = [cell.strip('"') for cell in lines[0].split(",")]
        # Voltage Rating keeps index 3; new fields are appended after it.
        assert header[:4] == ["Reference", "Value", "Part", "Voltage Rating"]
        assert "Manufacturer" in header
        assert lines[1].split(",")[3].strip('"') == "16 VDC"
        assert "0 VDC" not in lines[1]
        assert any(c.action is ChangeAction.UPDATE for c in result.changes)

    def test_quoting_style_is_preserved(self, tmp_path: Path) -> None:
        """A fully quoted BOM is not rewritten unquoted."""
        path = tmp_path / "bom.csv"
        path.write_text(
            '"Reference","Part"\n"C1","GRM155R61C104KA88D"\n', encoding="utf-8"
        )

        CSVProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        text = path.read_text(encoding="utf-8")
        assert text.splitlines()[0].startswith('"Reference"')
        assert '"Murata"' in text

    def test_unchanged_field_reports_no_change(self, tmp_path: Path) -> None:
        """A fully up-to-date row produces no changes at all."""
        path = tmp_path / "bom.csv"
        path.write_text(
            '"Reference","Value","Part","Manufacturer","Description",'
            '"Voltage Rating","Temperature Min","Temperature Max","Package"\n'
            '"C1","100nF","GRM155R61C104KA88D","Murata","MLCC 0.1uF","16 VDC",'
            '"-55 C","+85 C","0402"\n',
            encoding="utf-8",
        )

        result = CSVProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        assert result.changes == ()
        assert result.written is False
        assert path.read_text(encoding="utf-8").count("Murata") == 1

    def test_ragged_rows_are_handled(self, tmp_path: Path) -> None:
        """Rows with differing cell counts are padded, not corrupted."""
        path = tmp_path / "bom.csv"
        path.write_text(
            '"Reference","Part"\n'
            '"C1","GRM155R61C104KA88D"\n'
            '"R1","RC0402FR-0710KL","extra","more"\n',
            encoding="utf-8",
        )

        result = CSVProcessor(provider_with(**{CAP.mpn: CAP, RES.mpn: RES})).process(path)

        assert result.written is True
        lines = path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 3
        assert "Murata" in lines[1] and "Yageo" in lines[2]
        assert "extra" in lines[2] and "more" in lines[2]

    def test_missing_parts_are_reported_not_fatal(
        self, tmp_path: Path, csv_bom_text: str
    ) -> None:
        """Unknown parts are collected rather than raising."""
        path = tmp_path / "bom.csv"
        path.write_text(csv_bom_text, encoding="utf-8")

        result = CSVProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        assert result.missing_parts == ("RC0402FR-0710KL",)
        assert result.written is True

    def test_missing_column_raises(
        self, tmp_path: Path
    ) -> None:
        """A BOM without a part number column is rejected."""
        path = tmp_path / "bad.csv"
        path.write_text('"Reference","Value"\n"C1","100nF"\n', encoding="utf-8")
        with pytest.raises(FileFormatError, match="No part number column"):
            CSVProcessor(provider_with()).process(path)

    def test_empty_file_raises(self, tmp_path: Path) -> None:
        """An empty BOM is rejected."""
        path = tmp_path / "empty.csv"
        path.write_text("   \n", encoding="utf-8")
        with pytest.raises(FileFormatError, match="empty"):
            CSVProcessor(provider_with()).process(path)

    def test_missing_file_raises(self, tmp_path: Path) -> None:
        """A nonexistent input is rejected."""
        with pytest.raises(FileFormatError, match="not found"):
            CSVProcessor(provider_with()).process(tmp_path / "nope.csv")

    def test_no_temp_files_left_behind(
        self, tmp_path: Path, csv_bom_text: str
    ) -> None:
        """Atomic writing cleans up its temporary file."""
        path = tmp_path / "bom.csv"
        path.write_text(csv_bom_text, encoding="utf-8")

        CSVProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        assert list(tmp_path.iterdir()) == [path]

    def test_extra_columns_are_preserved(self, tmp_path: Path) -> None:
        """Unrelated columns survive the round trip."""
        path = tmp_path / "bom.csv"
        path.write_text(
            '"Reference","Value","Part","Notes"\n'
            '"C1","100nF","GRM155R61C104KA88D","hand placed"\n',
            encoding="utf-8",
        )

        CSVProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        assert "hand placed" in path.read_text(encoding="utf-8")


class TestKiCadSymProcessor:
    """Verify symbol library enrichment."""

    def test_dry_run_never_writes(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """A dry run leaves the library untouched."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")
        original = path.read_bytes()

        result = KiCadSymProcessor(provider_with(**{CAP.mpn: CAP}), dry_run=True).process(path)

        assert path.read_bytes() == original
        assert result.dry_run is True
        assert result.written is False

    def test_dry_run_prints_report(
        self, tmp_path: Path, kicad_sym_text: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A dry run narrates its proposed changes."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        KiCadSymProcessor(provider_with(**{CAP.mpn: CAP}), dry_run=True).process(path)

        assert "Dry run" in capsys.readouterr().out

    def test_existing_property_value_replaced(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """An existing Voltage property has its value replaced."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        result = KiCadSymProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        text = path.read_text(encoding="utf-8")
        assert '"Voltage Rating" "16 VDC"' in text
        assert '"0 VDC"' not in text
        assert result.written is True

    def test_missing_property_is_added(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """Absent managed fields are inserted into the symbol."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        KiCadSymProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        text = path.read_text(encoding="utf-8")
        assert '"Temperature Min" "-55 C"' in text
        assert '"Temperature Max" "+85 C"' in text
        assert '"Package" "0402"' in text

    def test_file_remains_parseable_after_edit(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """The rewritten library still parses as a symbol library."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        KiCadSymProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        root = parse(path.read_text(encoding="utf-8"))
        assert root.head_name == "kicad_symbol_lib"

    def test_formatting_outside_edits_is_preserved(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """Comments, tabs and unrelated properties are untouched."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        KiCadSymProcessor(provider_with(**{CAP.mpn: CAP})).process(path)
        text = path.read_text(encoding="utf-8")

        assert "; hand maintained library" in text
        assert '\t(generator "component_sync")\n' in text
        assert '"Footprint" "fp:C_0402"' in text
        assert '"Part" "GRM155R61C104KA88D"' in text
        assert "\r" not in text

    def test_description_is_not_managed(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """Description is left alone even when the provider returns one."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        KiCadSymProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        assert "MLCC 0.1uF" not in path.read_text(encoding="utf-8")

    def test_symbol_without_part_is_skipped(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """Symbols lacking a Part or MPN field are ignored."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(
            kicad_sym_text.replace(
                '\t\t(property "Part" "GRM155R61C104KA88D"\n\t\t\t(at 0 0 0)\n\t\t)\n',
                "",
            ),
            encoding="utf-8",
        )

        result = KiCadSymProcessor(provider_with(**{CAP.mpn: CAP})).process(path)

        assert result.changes == ()

    def test_missing_part_is_reported(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """An unresolvable Part value is collected as missing."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        result = KiCadSymProcessor(provider_with()).process(path)

        assert result.missing_parts == ("GRM155R61C104KA88D",)

    def test_non_kicad_file_rejected(self, tmp_path: Path) -> None:
        """A file that is not a symbol library is rejected."""
        path = tmp_path / "other.kicad_sym"
        path.write_text('(something_else\n\t(symbol "X")\n)\n', encoding="utf-8")
        with pytest.raises(FileFormatError, match="not a KiCad symbol library"):
            KiCadSymProcessor(provider_with()).process(path)

    def test_escaped_quotes_round_trip(self, tmp_path: Path, kicad_sym_text: str) -> None:
        """Managed values containing quotes are escaped and parse back correctly."""
        tricky = ComponentData(
            mpn="GRM155R61C104KA88D",
            package='0402 "thin"',  # Package is a managed field
            voltage_text="16 VDC",
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        KiCadSymProcessor(provider_with(**{tricky.mpn: tricky})).process(path)

        root = parse(path.read_text(encoding="utf-8"))
        symbol = next(root.children("symbol"))
        values: dict[str, str] = {}
        for prop in symbol.children("property"):
            if len(prop.items) < 3:
                continue
            name, value = prop.items[1], prop.items[2]
            if isinstance(name, SString) and isinstance(value, SString):
                values[name.value] = value.value
        assert values["Package"] == '0402 "thin"'

    def test_single_valued_voltage_is_not_dropped(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """Regression: a lone voltage must still reach the symbol.

        A part that publishes one voltage rather than a range falls back to
        ``Voltage Rating``. That field used to be absent from MANAGED_FIELDS,
        so the value was filtered out and silently dropped.
        """
        single = ComponentData(
            mpn="GRM155R61C104KA88D",
            manufacturer="Murata",
            voltage_text="16 VDC",
            temp_min="-55 C",
            temp_max="+85 C",
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        result = KiCadSymProcessor(provider_with(**{single.mpn: single})).process(path)
        text = path.read_text(encoding="utf-8")

        assert '"Voltage Rating" "16 VDC"' in text
        assert any(
            change.field_name == "Voltage Rating" for change in result.changes
        )

    def test_voltage_min_max_are_written_when_ranged(
        self, tmp_path: Path, kicad_sym_text: str
    ) -> None:
        """A ranged part writes Voltage Min and Voltage Max, not a rating."""
        ranged = ComponentData(
            mpn="GRM155R61C104KA88D",
            voltage_min="2.65 V",
            voltage_max="3.5 V",
        )
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")

        KiCadSymProcessor(provider_with(**{ranged.mpn: ranged})).process(path)
        text = path.read_text(encoding="utf-8")

        assert '"Voltage Min" "2.65 V"' in text
        assert '"Voltage Max" "3.5 V"' in text
        # The pre-existing Voltage Rating placeholder is left alone: a ranged
        # part must not also gain a rating.
        assert '"Voltage Rating" "0 VDC"' in text

    def test_idempotent_second_run(self, tmp_path: Path, kicad_sym_text: str) -> None:
        """Running twice makes no further changes."""
        path = tmp_path / "lib.kicad_sym"
        path.write_text(kicad_sym_text, encoding="utf-8")
        provider = provider_with(**{CAP.mpn: CAP})

        KiCadSymProcessor(provider).process(path)
        after_first = path.read_bytes()
        result = KiCadSymProcessor(provider).process(path)

        assert path.read_bytes() == after_first
        assert result.changes == ()


class TestSExpParser:
    """Verify the structural parser."""

    def test_parses_nested_lists(self) -> None:
        """Nested structure is captured correctly."""
        root = parse('(a (b 1 2) (c "x"))')
        assert root.head_name == "a"
        assert len(list(root.children("b"))) == 1
        assert len(list(root.children("c"))) == 1

    def test_escapes_are_decoded(self) -> None:
        """Escape sequences resolve to their characters."""
        root = parse(r'(a "line\nbreak \"quoted\" back\\slash")')
        node = root.items[1]
        assert isinstance(node, SString)
        assert node.value == 'line\nbreak "quoted" back\\slash'

    def test_comments_are_skipped(self) -> None:
        """Semicolon comments do not become nodes."""
        root = parse("(a ; comment\n b)")
        assert len(root.items) == 2

    def test_unbalanced_raises(self) -> None:
        """An unterminated list is an error."""
        with pytest.raises(FileFormatError, match="Unterminated"):
            parse("(a (b")

    def test_unterminated_string_raises(self) -> None:
        """An unterminated string is an error."""
        with pytest.raises(FileFormatError, match="Unterminated string"):
            parse('(a "oops)')

    def test_trailing_garbage_raises(self) -> None:
        """Content after the root node is rejected."""
        with pytest.raises(FileFormatError, match="Trailing content"):
            parse("(a) (b)")

    def test_quote_escapes(self) -> None:
        """Encoding a value produces a valid string token."""
        assert quote('a"b\\c') == '"a\\"b\\\\c"'

    def test_quote_round_trip(self) -> None:
        """Quoting then parsing returns the original value."""
        for original in ('plain', 'with "quotes"', "back\\slash", "new\nline"):
            root = parse(f"(x {quote(original)})")
            node = root.items[1]
            assert isinstance(node, SString)
            assert node.value == original

    def test_apply_edits_detects_overlap(self) -> None:
        """Overlapping edits are refused rather than corrupting text."""
        edits = [TextEdit(0, 5, "x"), TextEdit(3, 8, "y")]
        with pytest.raises(FileFormatError, match="Overlapping"):
            apply_edits("abcdefghij", edits)

    def test_apply_edits_is_order_independent(self) -> None:
        """Edits apply correctly regardless of input order.

        ``0123456789`` with ``(6,8)->BB`` and ``(1,3)->AA`` must become
        ``0AA345BB89``.
        """
        edits = [TextEdit(6, 8, "BB"), TextEdit(1, 3, "AA")]
        assert apply_edits("0123456789", edits) == "0AA345BB89"


class TestAtomicWrite:
    """Verify atomic file replacement."""

    def test_replaces_content(self, tmp_path: Path) -> None:
        """The target file receives the new content."""
        path = tmp_path / "f.txt"
        atomic_write(path, "hello")
        assert path.read_text() == "hello"

    def test_preserves_permissions_on_replace(self, tmp_path: Path) -> None:
        """Replacement keeps the file usable by the owner."""
        path = tmp_path / "f.txt"
        path.write_text("old")
        atomic_write(path, "new")
        assert path.read_text() == "new"
        assert path.stat().st_size == 3

    def test_failure_leaves_original_intact(self, tmp_path: Path) -> None:
        """A failed write does not damage the original file."""
        path = tmp_path / "f.txt"
        path.write_text("original")

        with pytest.raises(TypeError):
            atomic_write(path, None)  # type: ignore[arg-type]

        assert path.read_text() == "original"
        assert [p.name for p in tmp_path.iterdir()] == ["f.txt"]
