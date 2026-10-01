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

    Attributes:
        mpn: Manufacturer part number, used as the lookup key.
        manufacturer: Manufacturer name.
        description: Human readable part description.
        voltage: Supply or working voltage range as reported by the vendor.
        operating_temp: Operating temperature range as reported by the vendor.
        package: Package or case designation.
        raw_parameters: Every vendor parameter keyed by its normalised name.
            Values are preserved verbatim so that information which has no
            dedicated attribute above is never lost.
    """

    mpn: str
    manufacturer: str = ""
    description: str = ""
    voltage: str = ""
    operating_temp: str = ""
    package: str = ""
    raw_parameters: dict[str, str] = field(default_factory=dict)

    def as_properties(self) -> dict[str, str]:
        """Return the non-empty fields as a KiCad property mapping.

        Empty strings are omitted so that blank placeholders are never written
        into a symbol library.

        Returns:
            Mapping of KiCad field name to value, omitting empty values.
        """
        mapping = {
            "Manufacturer": self.manufacturer,
            "Description": self.description,
            "Voltage": self.voltage,
            "Operating Temperature": self.operating_temp,
            "Package": self.package,
        }
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
        """Return the number of fields whose value actually changed."""
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
