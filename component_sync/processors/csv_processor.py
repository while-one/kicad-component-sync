"""CSV bill-of-materials processor."""

from __future__ import annotations

import csv
import io
import logging
from pathlib import Path

from ..exceptions import FileFormatError
from ..models import ProcessResult, PropertyChange
from .base import BaseProcessor, atomic_write

__all__ = ["CSVProcessor"]

LOGGER = logging.getLogger(__name__)

#: Header aliases accepted for the manufacturer part number column, lowercased.
_MPN_ALIASES = ("mpn", "part", "part number", "manufacturer part number")


class CSVProcessor(BaseProcessor):
    """Enrich a delimited BOM by resolving the part number column.

    The processor preserves the original dialect and quoting style so that a
    round trip through it produces a minimal diff: the column order, extra
    columns, and quoting style of the input are all kept.
    """

    def process(self, file_path: Path, dry_run: bool = False) -> ProcessResult:
        """Resolve and write parametric fields for every BOM row.

        Args:
            file_path: Path to the CSV BOM file.
            dry_run: When true, report changes without touching the file.

        Returns:
            A report of applied or proposed changes and any unresolved parts.

        Raises:
            FileFormatError: If the file is unreadable, empty, or has no
                recognisable part number column.
        """
        dry = dry_run or self.dry_run
        if not file_path.is_file():
            raise FileFormatError(f"Input file not found: {file_path}")

        text = file_path.read_text(encoding="utf-8-sig")
        if not text.strip():
            raise FileFormatError(f"CSV file is empty: {file_path}")

        dialect: type[csv.Dialect] = csv.excel
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        # ``Sniffer`` detects the delimiter but not whether the author quoted
        # every field. Detect that from the header so a fully quoted BOM stays
        # fully quoted instead of silently being rewritten unquoted.
        quotechar = getattr(dialect, "quotechar", '"') or '"'
        if text.lstrip().startswith(quotechar):
            dialect.quoting = csv.QUOTE_ALL

        reader = csv.reader(io.StringIO(text), dialect)
        rows = list(reader)
        if not rows:
            raise FileFormatError(f"CSV file has no rows: {file_path}")

        header = [cell.strip() for cell in rows[0]]
        mpn_index = self._find_mpn_column(header)
        if mpn_index is None:
            raise FileFormatError(
                f"No part number column found in {file_path}. "
                f"Expected one of: {', '.join(_MPN_ALIASES)}"
            )

        changes: list[PropertyChange] = []
        missing: list[str] = []
        column_values: dict[int, dict[str, str]] = {}

        for row in rows[1:]:
            if not row or mpn_index >= len(row):
                continue
            mpn = row[mpn_index].strip()
            if not mpn:
                continue

            data = self._resolve(mpn)
            if data is None:
                if mpn not in missing:
                    missing.append(mpn)
                continue

            existing = {
                name: (row[i] if i < len(row) else "")
                for i, name in enumerate(header)
                if name and i != mpn_index
            }
            row_changes = self._classify(mpn, existing, data.as_properties())
            changes.extend(row_changes)

            if row_changes and not dry:
                wanted = {change.field_name: change.new_value for change in row_changes}
                column_values[id(row)] = wanted

        header, rows = self._apply(rows, header, mpn_index, column_values)

        if changes and not dry:
            rows[0] = header  # rows[0] is the header row itself
            buffer = io.StringIO()
            writer = csv.writer(buffer, dialect, quoting=dialect.quoting)
            writer.writerows(rows)
            atomic_write(file_path, buffer.getvalue())

        result = ProcessResult(
            file_path=str(file_path),
            changes=tuple(changes),
            missing_parts=tuple(missing),
            dry_run=dry,
            written=bool(changes) and not dry,
        )
        if dry:
            self._report(result)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _find_mpn_column(header: list[str]) -> int | None:
        """Return the index of the part number column, or ``None``.

        Args:
            header: Normalised header row.

        Returns:
            Column index of the manufacturer part number.
        """
        for index, name in enumerate(header):
            if name.strip().casefold() in _MPN_ALIASES:
                return index
        return None

    @staticmethod
    def _apply(
        rows: list[list[str]],
        header: list[str],
        mpn_index: int,
        column_values: dict[int, dict[str, str]],
    ) -> tuple[list[str], list[list[str]]]:
        """Return the header and rows with new fields materialised in place.

        Rows in a CSV need not all have the same number of cells. New columns are
        therefore appended *after the widest row* rather than directly after the
        header, so any cells a ragged row already carries are never overwritten.

        Args:
            rows: All CSV rows including the header.
            header: Normalised header cells.
            mpn_index: Index of the part number column, which is never
                overwritten by a managed field.
            column_values: Mapping of ``id(row)`` to the fields to write.

        Returns:
            The updated header and rows.
        """
        additions: list[str] = []
        for row in rows[1:]:
            for name in column_values.get(id(row), {}):
                if name not in header and name not in additions:
                    additions.append(name)

        widest = max((len(row) for row in rows), default=len(header))
        padding = [""] * max(0, widest - len(header))
        new_header = [*header, *padding, *additions]
        width = len(new_header)

        for row in rows[1:]:
            wanted = column_values.get(id(row))
            if not wanted:
                continue
            while len(row) < width:
                row.append("")
            for name, value in wanted.items():
                index = new_header.index(name)
                if index == mpn_index:
                    continue  # never clobber the part number column
                row[index] = value
        return new_header, rows

    @staticmethod
    def _report(result: ProcessResult) -> None:
        """Print the proposed changes for a dry run.

        Args:
            result: The result being reported.
        """
        print("Dry run: no files were modified.")
        print("Proposed changes:")
        if result.changes:
            for change in result.changes:
                print(change.describe())
        else:
            print("  (none - BOM is already up to date)")
        print()
        print(result.summary())
