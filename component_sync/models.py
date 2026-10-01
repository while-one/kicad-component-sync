"""Standardised data models shared across providers and processors.

The :class:`ComponentData` dataclass is the single contract between a
:class:`~component_sync.providers.base.BaseProvider` and any consumer of its
results. Providers normalise vendor-specific payloads into this shape so the
processors never need to know which distributor answered.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

__all__ = ["ComponentData", "ProcessResult", "PropertyChange", "ChangeAction"]


class ChangeAction(Enum):
    """How a proposed property change relates to the current file state.

    Attributes:
        ADD: The field does not exist yet and will be created.
        UPDATE: The field exists and its value will be replaced.
        UNCHANGED: The field already holds the desired value.
        SKIP: The field is excluded from synchronisation.
    """

    ADD = "add"
    UPDATE = "update"
    UNCHANGED = "unchanged"
    SKIP = "skip"


@dataclass(frozen=True)
class ComponentData:
    """Normalised parametric data for a single electronic component.

    Ranges are carried as separate bounds rather than one free-text string, so
    that they can be written using the project's established field names
    (``Voltage Min`` / ``Voltage Max`` / ``Temperature Min`` /
    ``Temperature Max``) rather than a single ambiguous field.

    Attributes:
        mpn: Manufacturer part number, used as the lookup key.
        manufacturer: Manufacturer name.
        description: Human readable part description.
        datasheet: Datasheet URL or local filename as published by the vendor.
        value: Display label for the ``Value`` field, for example ``100 pF``.
            Left empty when the vendor publishes no quantity that determines a
            label, so that an existing human-chosen value is never destroyed.
        voltage_min: Lower bound of the supply or working voltage.
        voltage_max: Upper bound of the supply or working voltage.
        voltage_text: Verbatim voltage string from the vendor, used only when a
            single value or free text is returned and no bounds can be derived.
        temp_min: Lower bound of the operating temperature.
        temp_max: Upper bound of the operating temperature.
        temp_text: Verbatim temperature string, used only when a single value or
            free text is returned and no bounds can be derived.
        package: Package or case designation.
        digikey_url: Product page URL at DigiKey, exactly as returned by the
            provider. Never synthesised, because a fabricated URL is
            indistinguishable from a real one in the symbol library.
        raw_parameters: Every vendor parameter keyed by its normalised name.
            Values are preserved verbatim so that information which has no
            dedicated attribute above is never lost.
    """

    mpn: str
    manufacturer: str = ""
    description: str = ""
    datasheet: str = ""
    value: str = ""
    voltage_min: str = ""
    voltage_max: str = ""
    voltage_text: str = ""
    temp_min: str = ""
    temp_max: str = ""
    temp_text: str = ""
    package: str = ""
    digikey_url: str = ""
    raw_parameters: dict[str, str] = field(default_factory=dict)

    @staticmethod
    def _fmt_bound(value: float, unit: str, *, temperature: bool) -> str:
        """Format one bound canonically.

        The absolute value is rendered and the sign is applied separately, so a
        negative bound never produces a doubled sign such as ``--55 C``.

        Args:
            value: The numeric bound.
            unit: Unit suffix, for example ``"C"`` or ``"V"``.
            temperature: When true, an explicit ``+`` is used for non-negative
                values, matching the ``+125 C`` convention used in symbol
                libraries.

        Returns:
            The formatted bound, or ``""`` when the value is not finite.
        """
        if value != value or value in (float("inf"), float("-inf")):
            return ""
        rounded = round(value, 3)
        magnitude = abs(rounded)
        if magnitude == int(magnitude):
            rendered = str(int(magnitude))
        else:
            rendered = f"{magnitude:g}"
        if temperature:
            sign = "+" if rounded >= 0 else "-"
            return f"{sign}{rendered} {unit}"
        return f"{rendered} {unit}"

    def voltage_properties(self) -> dict[str, str]:
        """Return voltage fields using the project's Min/Max naming.

        When both bounds are known the output is ``Voltage Min`` and
        ``Voltage Max``. When the vendor returned a single value or free text
        that cannot be split, the verbatim text is returned under
        ``Voltage Rating`` rather than inventing a bound.

        Returns:
            Mapping of field name to value, omitting empty values.
        """
        if self.voltage_min and self.voltage_max:
            return {"Voltage Min": self.voltage_min, "Voltage Max": self.voltage_max}
        if self.voltage_text:
            return {"Voltage Rating": self.voltage_text}
        return {}

    def temperature_properties(self) -> dict[str, str]:
        """Return temperature fields using the project's Min/Max naming.

        Returns:
            Mapping of field name to value, omitting empty values.
        """
        if self.temp_min and self.temp_max:
            return {"Temperature Min": self.temp_min, "Temperature Max": self.temp_max}
        if self.temp_text:
            return {"Operating Temperature": self.temp_text}
        return {}

    def as_properties(self) -> dict[str, str]:
        """Return every non-empty field as a KiCad property mapping.

        Field names follow the project convention: temperature and voltage
        ranges are split into ``Min``/``Max`` pairs. Empty strings are omitted so
        that blank placeholders are never written into a symbol library.

        Returns:
            Mapping of field name to value, omitting empty values.
        """
        mapping = {
            "Manufacturer": self.manufacturer,
            "Description": self.description,
            "Datasheet": self.datasheet,
            "Package": self.package,
        }
        if self.value:
            mapping["Value"] = self.value
        if self.digikey_url:
            mapping["Digikey"] = self.digikey_url
        mapping.update(self.voltage_properties())
        mapping.update(self.temperature_properties())
        return {key: value for key, value in mapping.items() if value.strip()}


@dataclass(frozen=True)
class PropertyChange:
    """A single proposed mutation of one field on one component.

    Attributes:
        identifier: MPN for CSV rows, or symbol name for KiCad symbols.
        field_name: Target field name.
        old_value: Current value, or ``None`` when the field will be added.
        new_value: Desired value.
        action: Classification of the change.
    """

    identifier: str
    field_name: str
    old_value: str | None
    new_value: str
    action: ChangeAction

    def describe(self) -> str:
        """Return a one line, human readable summary of this change.

        Returns:
            A summary suitable for printing during a dry run.
        """
        target = f"{self.identifier}"
        if self.action is ChangeAction.ADD:
            return f"  + {target}: {self.field_name} = {self.new_value!r} (added)"
        if self.action is ChangeAction.UPDATE:
            return (
                f"  ~ {target}: {self.field_name} "
                f"{self.old_value!r} -> {self.new_value!r}"
            )
        return f"  = {target}: {self.field_name} already {self.new_value!r}"


@dataclass(frozen=True)
class ProcessResult:
    """Outcome of running a processor over an input file.

    Attributes:
        file_path: The file that was processed.
        changes: Property mutations that were applied or proposed.
        missing_parts: MPNs the provider could not resolve.
        dry_run: Whether the run was non-destructive.
        written: Whether the file was actually replaced on disk.
    """

    file_path: str
    changes: tuple[PropertyChange, ...] = ()
    missing_parts: tuple[str, ...] = ()
    dry_run: bool = False
    written: bool = False

    @property
    def modified_count(self) -> int:
        """Return the number of fields whose value would change."""
        return sum(
            1 for change in self.changes if change.action is not ChangeAction.UNCHANGED
        )

    def summary(self) -> str:
        """Return a multi line summary suitable for printing to stdout.

        Returns:
            A formatted report of the run.
        """
        lines = [
            f"File        : {self.file_path}",
            f"Mode        : {'dry-run (no changes written)' if self.dry_run else 'write'}",
            f"Changes     : {self.modified_count}",
            f"Missing     : {len(self.missing_parts)}",
            f"Written     : {'yes' if self.written else 'no'}",
        ]
        if self.missing_parts:
            lines.append("")
            lines.append("Parts not found:")
            lines.extend(f"  ! {mpn}" for mpn in self.missing_parts)
        return "\n".join(lines)
