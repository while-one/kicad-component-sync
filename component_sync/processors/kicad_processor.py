"""KiCad symbol library (``.kicad_sym``) processor.

Structural edits are performed on a parsed S-expression tree
(:mod:`component_sync.sexpr`) and applied as byte-span splices. This keeps the
file's indentation, comments, and property ordering untouched outside the exact
ranges that change, and it means the writer never rewrites the whole document.

``Description`` is skipped because KiCad derives it and because editing it
frequently triggers a full symbol re-render in the editor. Voltage, temperature
and package are the fields this processor maintains.
"""

from __future__ import annotations

import logging
from pathlib import Path

from ..exceptions import FileFormatError
from ..models import ProcessResult, PropertyChange
from ..sexpr import SList, SString, TextEdit, apply_edits, parse, quote
from .base import BaseProcessor, atomic_write

__all__ = ["KiCadSymProcessor"]

LOGGER = logging.getLogger(__name__)

#: Fields this processor owns, following the project's Min/Max convention.
#: ``Reference``, ``Footprint`` and ``Part`` are never written: they are the
#: designer's decisions, and ``Part`` is the lookup key itself.
MANAGED_FIELDS = (
    "Value",
    "Manufacturer",
    "Description",
    "Datasheet",
    "Package",
    "Digikey",
    "Voltage Min",
    "Voltage Max",
    "Voltage Rating",
    "Temperature Min",
    "Temperature Max",
    "Operating Temperature",
)

#: KiCad writes these fields at a zeroed position because they are hidden.
#: Placement used for properties this tool adds.
#:
#: ``(at 0 0 0)`` parks the field at the origin and ``(hide yes)`` keeps it off
#: the schematic. Without ``(hide yes)`` KiCad renders the field's text on the
#: sheet, so a run that added ``Digikey`` and ``Voltage Rating`` to 55 symbols
#: would bury the drawing under fields the author never asked to see. Fields that
#: already exist keep whatever visibility they were given, because an edit only
#: splices the value and never rewrites the surrounding block.
_HIDDEN_AT = "(at 0 0 0)"


class KiCadSymProcessor(BaseProcessor):
    """Enrich a KiCad 8+ symbol library with distributor parametric data.

    The library is parsed structurally. For each top-level ``symbol`` the
    ``Part`` field supplies the manufacturer part number, the matching part is
    resolved through the provider, and the managed fields are updated or added.

    Properties are never reordered or deleted: an existing field keeps its
    position and only its value is substituted, and a missing field is inserted
    immediately after the last existing property of that symbol.
    """

    def process(self, file_path: Path, dry_run: bool = False) -> ProcessResult:
        """Resolve and write parametric fields for every symbol.

        Args:
            file_path: Path to the ``.kicad_sym`` library.
            dry_run: When true, report changes without touching the file.

        Returns:
            A report of applied or proposed changes and any unresolved parts.

        Raises:
            FileFormatError: If the file cannot be parsed as a symbol library.
        """
        dry = dry_run or self.dry_run
        if not file_path.is_file():
            raise FileFormatError(f"Input file not found: {file_path}")

        text = file_path.read_text(encoding="utf-8")
        try:
            root = parse(text)
        except FileFormatError:
            raise
        except Exception as exc:  # noqa: BLE001 - normalise parser errors
            raise FileFormatError(f"Could not parse {file_path}: {exc}") from exc

        if root.head_name != "kicad_symbol_lib":
            raise FileFormatError(
                f"{file_path} is not a KiCad symbol library "
                f"(root node is {root.head_name!r})"
            )

        edits: list[TextEdit] = []
        changes = []
        missing: list[str] = []
        seen: set[str] = set()

        for symbol in root.children("symbol"):
            name_node = symbol.items[1] if len(symbol.items) > 1 else None
            if not isinstance(name_node, SString):
                continue
            symbol_name = name_node.value

            mpn = self._read_property(symbol, "Part") or self._read_property(
                symbol, "MPN"
            )
            if not mpn.strip():
                continue
            mpn = mpn.strip()
            seen.add(mpn)

            data = self._resolve(mpn)
            if data is None:
                if mpn not in missing:
                    missing.append(mpn)
                continue

            desired = {
                name: value
                for name, value in data.as_properties().items()
                if name in MANAGED_FIELDS
            }
            existing = self._properties(symbol)
            row_changes = self._classify(symbol_name, existing, desired)
            changes.extend(row_changes)

            if row_changes and not dry:
                edits.extend(self._edits_for(symbol, text, existing, row_changes))

        if edits:
            atomic_write(file_path, apply_edits(text, edits))
            LOGGER.info("Applied %d span edits to %s", len(edits), file_path)

        result = ProcessResult(
            file_path=str(file_path),
            changes=tuple(changes),
            missing_parts=tuple(missing),
            dry_run=dry,
            written=bool(edits),
            lookups=self.cache.stats.describe(),
        )
        if dry:
            self._report(result)
        return result

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------
    @staticmethod
    def _read_property(symbol: SList, name: str) -> str:
        """Return the value of a property, or ``""`` when absent.

        Args:
            symbol: The symbol node to search.
            name: Property name to read.

        Returns:
            The decoded property value.
        """
        for prop in symbol.children("property"):
            items = prop.items
            if len(items) >= 3 and isinstance(items[1], SString) and items[1].value == name:
                return items[2].value if isinstance(items[2], SString) else ""
        return ""

    @staticmethod
    def _properties(symbol: SList) -> dict[str, str]:
        """Return every property of a symbol as a name to value mapping.

        Args:
            symbol: The symbol node to inspect.

        Returns:
            Mapping of property name to value.
        """
        found: dict[str, str] = {}
        for prop in symbol.children("property"):
            items = prop.items
            if len(items) >= 3 and isinstance(items[1], SString):
                found[items[1].value] = (
                    items[2].value if isinstance(items[2], SString) else ""
                )
        return found

    @staticmethod
    def _edits_for(
        symbol: SList,
        text: str,
        existing: dict[str, str],
        changes: list[PropertyChange],
    ) -> list[TextEdit]:
        """Build the span edits that apply one symbol's changes.

        Args:
            symbol: The symbol node being modified.
            text: The full source document.
            existing: Current property values for the symbol.
            changes: Proposed changes for this symbol.

        Returns:
            The edits needed to realise the changes.

        Raises:
            FileFormatError: If a property has an unexpected shape.
        """
        edits: list[TextEdit] = []
        to_add: list[tuple[str, str]] = []

        for change in changes:
            if change.old_value is not None:
                value_node = KiCadSymProcessor._value_node(symbol, change.field_name)
                if value_node is None:
                    raise FileFormatError(
                        f"Could not locate property {change.field_name!r} for splice"
                    )
                edits.append(
                    TextEdit(
                        start=value_node.start,
                        end=value_node.end,
                        replacement=quote(change.new_value),
                    )
                )
            else:
                to_add.append((change.field_name, change.new_value))

        if to_add:
            anchor = KiCadSymProcessor._insert_anchor(symbol, text)
            block = "".join(
                f"\t\t(property {quote(name)} {quote(value)}\n"
                f"\t\t\t{_HIDDEN_AT}\n"
                f"\t\t\t(show_name no)\n"
                f"\t\t\t(do_not_autoplace no)\n"
                f"\t\t\t(hide yes)\n"
                f"\t\t\t(effects\n"
                f"\t\t\t\t(font\n"
                f"\t\t\t\t\t(size 1.27 1.27)\n"
                f"\t\t\t\t)\n"
                f"\t\t\t)\n"
                f"\t\t)\n"
                for name, value in to_add
            )
            edits.append(TextEdit(start=anchor, end=anchor, replacement=block))

        _ = existing
        return edits


    @staticmethod
    def _value_node(symbol: SList, name: str) -> SString | None:
        """Return the value node of a named property.

        Args:
            symbol: The symbol node to inspect.
            name: Property name.

        Returns:
            The value string node, or ``None`` when the property is absent.
        """
        for prop in symbol.children("property"):
            items = prop.items
            if len(items) >= 3 and isinstance(items[1], SString) and items[1].value == name:
                return items[2] if isinstance(items[2], SString) else None
        return None

    @staticmethod
    def _insert_anchor(symbol: SList, text: str) -> int:
        """Return the offset at which new properties should be inserted.

        The anchor is just after the final existing ``property`` block so that
        additions keep the file's natural field ordering.

        Args:
            symbol: The symbol node being modified.
            text: The full source document, used to place the newline.

        Returns:
            Character offset for the insertion.
        """
        last: SList | None = None
        for prop in symbol.children("property"):
            last = prop
        if last is None:
            # No properties yet: insert before the first child list.
            for item in symbol.items[1:]:
                if isinstance(item, SList):
                    return item.start
            return symbol.end - 1

        offset = last.end
        if offset < len(text) and text[offset] == "\n":
            offset += 1
        return offset

