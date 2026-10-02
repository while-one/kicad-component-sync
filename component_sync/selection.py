"""Field selection and change classification for reviewable output.

A run over a real library produces hundreds of changes spanning very different
levels of risk. Filling an empty ``Datasheet`` field is safe; replacing a
description the author wrote by hand is not; and a handful of ``Value`` changes
implement a deliberate convention. Reporting those as one undifferentiated wall
forces the author to read all 264 lines to find the 9 that matter.

This module holds the two decisions that make the report reviewable: which
fields a run is allowed to touch, and which resulting changes are worth a human
decision.
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import PropertyChange

__all__ = ["FieldSelection", "ComponentFilter", "split_field_list"]


@dataclass(frozen=True)
class ComponentFilter:
    """Restricts a run to the components whose name or part number matches.

    Selecting a single component is a normal thing to want when a library holds
    fifty-eight symbols, and it is worth more than a convenience here: the
    filter is applied *before* any provider lookup, so a one-component run costs
    one API request rather than one per part. With DigiKey's daily quota at
    1000 requests, that difference decides whether an exploratory run is
    affordable at all.

    Matching is a case-insensitive substring test against both the symbol name
    and the manufacturer part number, so ``--component 1028`` finds the symbol
    called ``1028`` and the one whose part number contains ``1028``, without the
    caller needing to know which of the two a given library uses.

    Attributes:
        patterns: Substrings to match. An empty tuple admits every component.
    """

    patterns: tuple[str, ...] = ()

    @classmethod
    def build(cls, text: str | None) -> ComponentFilter:
        """Construct a filter from a comma separated argument.

        Args:
            text: Raw ``--component`` text, or ``None`` for no filtering.

        Returns:
            The configured filter.
        """
        if not text:
            return cls()
        parts = [part.strip() for part in text.split(",")]
        return cls(tuple(part for part in parts if part))

    def admits(self, name: str, mpn: str = "") -> bool:
        """Return whether a component is in scope for this run.

        Args:
            name: The symbol name, or an empty string for a CSV row.
            mpn: The manufacturer part number.

        Returns:
            True when no filter is set, or when either the name or the part
            number contains one of the patterns.
        """
        if not self.patterns:
            return True
        haystacks = (name, mpn)
        return any(
            pattern.casefold() in haystack.casefold()
            for pattern in self.patterns
            for haystack in haystacks
            if haystack
        )

    def is_active(self) -> bool:
        """Return whether any pattern is set.

        Returns:
            True when the filter would exclude something.
        """
        return bool(self.patterns)

    def describe(self) -> str:
        """Return a one line description for the report header.

        Returns:
            The active patterns, or ``""`` when nothing is filtered.
        """
        return ", ".join(self.patterns)


def split_field_list(text: str) -> frozenset[str]:
    """Parse a comma separated list of field names.

    Names are matched case-insensitively, because the user types ``--only value``
    and the library stores ``Value``. Whitespace around each name is ignored and
    empty entries are dropped, so a trailing comma is harmless.

    Args:
        text: Raw argument text, for example ``"Value, Package"``.

    Returns:
        The field names, case-folded.
    """
    return frozenset(
        part.strip().casefold() for part in text.split(",") if part.strip()
    )


@dataclass
class FieldSelection:
    """Which fields a run may touch, and how duplicates are treated.

    ``only`` and ``skip`` are mutually exclusive in effect: when ``only`` is set
    it wins, so ``--only Value --skip Package`` still processes ``Value``.

    The filter is applied while changes are being classified, which means a
    withheld change never reaches the report and would otherwise be invisible.
    Suppressed fields are therefore accumulated here as the run proceeds, so the
    report can state what it held back rather than silently showing less.

    Attributes:
        only: When non-empty, the only fields permitted to change.
        skip: Field names that are never touched.
        ignore_case: When true, a change that differs only in letter case is
            treated as no change at all. ``Yageo`` becoming ``YAGEO`` is a real
            difference in the file, but it is never worth a review decision.
    """

    only: frozenset[str] = frozenset()
    skip: frozenset[str] = frozenset()
    ignore_case: bool = False

    def __post_init__(self) -> None:
        """Initialise the suppressed-field accumulator.

        The field is created here rather than declared as a dataclass attribute
        so it never appears in ``__eq__`` or ``__repr__``: two selections with
        the same configuration are equal regardless of what a run has done.
        """
        self._suppressed: set[str] = set()

    @classmethod
    def build(
        cls,
        *,
        only: str | None = None,
        skip: str | None = None,
        ignore_case: bool = False,
    ) -> FieldSelection:
        """Construct a selection from raw command line arguments.

        Args:
            only: Raw ``--only`` text, or ``None``.
            skip: Raw ``--skip`` text, or ``None``.
            ignore_case: Whether to suppress case-only differences.

        Returns:
            The configured selection.
        """
        return cls(
            only=split_field_list(only) if only else frozenset(),
            skip=split_field_list(skip) if skip else frozenset(),
            ignore_case=ignore_case,
        )

    def admits(self, field: str) -> bool:
        """Return whether a field is permitted to change in this run.

        Args:
            field: The target field name.

        Returns:
            True when the field passes the ``only`` and ``skip`` filters.
        """
        key = field.strip().casefold()
        if self.only:
            return key in self.only
        return key not in self.skip

    def is_cosmetic(self, old: str | None, new: str) -> bool:
        """Return whether a change is case-only and so ignorable.

        Comparison is on the stripped, case-folded strings, so leading and
        trailing whitespace differences are absorbed too. A field that did not
        previously exist is never cosmetic, even when its value is trivially
        short: adding a field is a real change.

        Args:
            old: The current value, or ``None`` when the field is being added.
            new: The desired value.

        Returns:
            True when the two values differ only in case or surrounding space.
        """
        if not self.ignore_case or old is None:
            return False
        return old.strip().casefold() == new.strip().casefold()

    def filter_changes(
        self, changes: tuple[PropertyChange, ...]
    ) -> tuple[PropertyChange, ...]:
        """Return the changes this selection permits, dropping cosmetic ones.

        Args:
            changes: Every change the processor proposed.

        Returns:
            The changes that survive both filters, in their original order.
        """
        kept: list[PropertyChange] = []
        for change in changes:
            if not self.admits(change.field_name):
                self.note_suppressed(change.field_name)
                continue
            if self.is_cosmetic(change.old_value, change.new_value):
                self.note_suppressed(change.field_name)
                continue
            kept.append(change)
        return tuple(kept)

    def note_suppressed(self, field: str) -> None:
        """Record that a field was withheld, so the report can disclose it.

        A run that quietly drops 48 changes is indistinguishable from a run that
        found nothing to do. The processor calls this as it filters, because by
        the time a change reaches the report the excluded ones no longer exist
        anywhere.

        Args:
            field: The field name that was withheld.
        """
        self._suppressed.add(field)

    def suppressed_fields(self) -> tuple[str, ...]:
        """Return the field names withheld during this run.

        Returns:
            Sorted names of fields excluded by ``only``, ``skip`` or
            ``ignore_case``, empty when nothing was withheld.
        """
        return tuple(sorted(self._suppressed))

    def describe(self) -> str:
        """Return a one line description of the active filters.

        Returns:
            A summary suitable for the report header, or ``""`` when no filter
            is active and there is nothing to say.
        """
        parts: list[str] = []
        if self.only:
            parts.append(f"only: {', '.join(sorted(self.only))}")
        if self.skip:
            parts.append(f"skip: {', '.join(sorted(self.skip))}")
        if self.ignore_case:
            parts.append("ignore-case")
        return "  ".join(parts)

    def is_active(self) -> bool:
        """Return whether any filter is in force.

        Returns:
            True when at least one of ``only``, ``skip`` or ``ignore_case`` is
            set.
        """
        return bool(self.only or self.skip or self.ignore_case)
