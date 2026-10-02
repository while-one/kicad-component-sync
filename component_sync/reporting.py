"""Grouped, colour-aware reporting for a review run.

A dry run over a real library reports a few hundred changes. Presented as one
undifferentiated list, the handful that implement a deliberate convention are
invisible among the routine field fills, and the author has to read everything to
find them.

The report therefore leads with counts, then separates changes by how much
attention they actually deserve:

``NEEDS REVIEW``
    Overwrites a value that already exists. Someone chose that text.
``VALUE CONVENTION``
    The deliberate ``Value`` standardisation, called out on its own.
``SAFE ADDS``
    Fills an empty field. Nothing is lost if the value is wrong.
``UNRESOLVED``
    Parts the distributor does not stock, which need an action of their own.

Colour is used only when writing to a terminal, so redirecting to a file yields
plain text with no escape sequences to strip.
"""

from __future__ import annotations

import os
import sys
from typing import TextIO

from .models import ChangeAction, ProcessResult, PropertyChange
from .selection import FieldSelection

__all__ = ["Palette", "render_report", "default_palette"]


class Palette:
    """ANSI colour codes, or empty strings when colour is disabled.

    Attributes:
        enabled: Whether codes should be emitted at all.
    """

    #: Section heading.
    HEADING = "\033[1m"
    #: An overwrite of existing data.
    OVERWRITE = "\033[31m"
    #: A newly added field.
    ADD = "\033[32m"
    #: A part that could not be resolved.
    WARN = "\033[33m"
    #: Secondary detail such as counts and field names.
    DIM = "\033[2m"
    #: The deliberate Value standardisation.
    HIGHLIGHT = "\033[36m"
    #: Reset to the terminal default.
    RESET = "\033[0m"

    def __init__(self, enabled: bool) -> None:
        """Initialise the palette.

        Args:
            enabled: When false every code resolves to an empty string.
        """
        self.enabled = enabled

    def __call__(self, code: str, text: str) -> str:
        """Wrap ``text`` in a colour code when colour is enabled.

        Args:
            code: One of the class level code constants.
            text: The text to colour.

        Returns:
            The coloured text, or the text unchanged when colour is disabled.
        """
        if not self.enabled or not text:
            return text
        return f"{code}{text}{self.RESET}"


def default_palette(stream: TextIO | None = None, *, force: bool | None = None) -> Palette:
    """Return a palette suited to where the report is going.

    Colour is enabled when the destination is a terminal. The ``NO_COLOR``
    convention is honoured, and ``force`` overrides the detection entirely so a
    test or a ``--no-color`` flag can force plain output.

    Args:
        stream: The destination stream, defaulting to standard output.
        force: Explicit override; ``True`` forces colour on, ``False`` off.

    Returns:
        A palette configured for the destination.
    """
    if force is not None:
        return Palette(force)
    if os.environ.get("NO_COLOR") is not None:
        return Palette(False)
    target = sys.stdout if stream is None else stream
    try:
        return Palette(bool(target.isatty()))
    except (AttributeError, ValueError):  # pragma: no cover - closed stream
        return Palette(False)


def _truncate(text: str, width: int) -> str:
    """Shorten ``text`` to ``width`` characters by eliding its middle.

    Datasheet URLs from some manufacturers run past 250 characters, which
    destroys the alignment of every other column. The full value is still
    written to the library, so nothing is lost by abbreviating the report.

    The middle is elided rather than the tail because the report's purpose is to
    show what *changed*. Two DigiKey URLs for the same part share a long common
    prefix and differ only in the trailing product identifier, so eliding the end
    renders both sides as the same string and the change becomes invisible. The
    head and tail together are what distinguish them.

    Args:
        text: The value to render.
        width: Maximum length before eliding.

    Returns:
        The original text, or a shortened form with an ellipsis in the middle.
    """
    if len(text) <= width or width < 5:
        return text
    keep = width - 1
    head = keep // 2 + keep % 2
    return f"{text[:head]}…{text[len(text) - (keep - head):]}"


def _counts(changes: tuple[PropertyChange, ...]) -> dict[str, int]:
    """Return how many changes affect each field.

    Args:
        changes: The changes being reported.

    Returns:
        Mapping of field name to occurrence count.
    """
    tally: dict[str, int] = {}
    for change in changes:
        tally[change.field_name] = tally.get(change.field_name, 0) + 1
    return tally


def _field_breakdown(palette: Palette, changes: tuple[PropertyChange, ...]) -> str:
    """Return a one line, per-field count summary.

    Args:
        palette: Colour helper.
        changes: The changes being summarised.

    Returns:
        A line such as ``Description  48   Package  40``.
    """
    tally = _counts(changes)
    if not tally:
        return ""
    parts = [f"{name} {count}" for name, count in sorted(tally.items(), key=lambda kv: -kv[1])]
    return palette(palette.DIM, "   ".join(parts))


def _format_change(palette: Palette, change: PropertyChange, width: int) -> str:
    """Render one change as an indented field line.

    Args:
        palette: Colour helper.
        change: The change to render.
        width: Maximum width for each value.

    Returns:
        A formatted line without its trailing newline.
    """
    if change.action is ChangeAction.ADD:
        value = _truncate(change.new_value, width)
        return f"        {palette(palette.DIM, change.field_name)} = {value!r}"
    old = _truncate(change.old_value or "", width)
    new = _truncate(change.new_value, width)
    name = palette(palette.DIM, change.field_name)
    return f"        {name} {old!r} -> {new!r}"


def _format_group(
    palette: Palette,
    identifier: str,
    changes: list[PropertyChange],
    width: int,
) -> list[str]:
    """Render every change for one part as a single block.

    A part usually needs several fields touched, and listing each as its own
    top-level line separated by blank rows makes the reader reassemble which
    changes belong together. Grouping by part keeps a part's fields adjacent and
    lets the identifier be printed once.

    Args:
        palette: Colour helper.
        identifier: The part or symbol name.
        changes: Every change for this part, in report order.
        width: Maximum width for each value.

    Returns:
        The lines of the block, without a trailing blank line.
    """
    marker = palette(palette.ADD, "+") if all(
        change.action is ChangeAction.ADD for change in changes
    ) else palette(palette.OVERWRITE, "~")
    lines = [f"  {marker} {identifier}"]
    lines.extend(_format_change(palette, change, width) for change in changes)
    return lines


def _format_section(
    palette: Palette,
    heading: str,
    heading_code: str,
    changes: tuple[PropertyChange, ...],
    width: int,
) -> list[str]:
    """Render one report section, grouped by part.

    A part that appears in more than one section is listed in each, since the
    sections answer different questions and merging them would blur the risk
    distinction the report is built on.

    Args:
        palette: Colour helper.
        heading: The section title, already counting its changes.
        heading_code: Colour code for the heading.
        changes: The changes in this section.
        width: Maximum width for each value.

    Returns:
        The lines of the section, ending with a blank separator.
    """
    lines = [palette(heading_code, heading)]
    breakdown = _field_breakdown(palette, changes)
    if breakdown:
        lines.append(f"  {breakdown}")
    lines.append("")

    grouped: dict[str, list[PropertyChange]] = {}
    for change in changes:
        grouped.setdefault(change.identifier, []).append(change)

    for identifier, group in grouped.items():
        lines.extend(_format_group(palette, identifier, group, width))
        lines.append("")
    return lines


def render_report(
    result: ProcessResult,
    *,
    palette: Palette | None = None,
    selection: FieldSelection | None = None,
    width: int = 60,
) -> str:
    """Build the full grouped report for a run.

    Args:
        result: The outcome of the run.
        palette: Colour helper, defaulting to one suited to standard output.
        selection: The active field selection, disclosed in the footer.
        width: Maximum width for each rendered value.

    Returns:
        The complete report as a single string.
    """
    pal = palette if palette is not None else default_palette()
    sel = selection if selection is not None else FieldSelection()

    changes = result.changes
    values = tuple(c for c in changes if c.field_name == "Value")
    rest = tuple(c for c in changes if c.field_name != "Value")
    overwrites = tuple(c for c in rest if c.action is not ChangeAction.ADD)
    adds = tuple(c for c in rest if c.action is ChangeAction.ADD)

    lines: list[str] = []
    lines.append(pal(pal.HEADING, f"DRY RUN  {result.file_path}"))
    if result.components_examined is not None:
        lines.append(
            pal(
                pal.DIM,
                f"  filter: {result.components_examined} component(s) in scope",
            )
        )
    lines.append("")

    # The counts first: this is the part that answers "what needs attention".
    lines.append(f"  needs review  {len(overwrites):>4}  overwrites a value you already have")
    lines.append(f"  value edits   {len(values):>4}  SI convention, see below")
    lines.append(f"  safe adds     {len(adds):>4}  fills an empty field")
    if result.failed_parts:
        lines.append(
            f"  not queried   {len(result.failed_parts):>4}  provider error, retry may work"
        )
    lines.append(f"  unresolved    {len(result.missing_parts):>4}  not stocked, needs action")
    lines.append("")

    if overwrites:
        lines.extend(
            _format_section(
                pal, f"NEEDS REVIEW  ({len(overwrites)})", pal.OVERWRITE, overwrites, width
            )
        )

    if values:
        lines.extend(
            _format_section(
                pal,
                f"VALUE CONVENTION  ({len(values)})",
                pal.HIGHLIGHT,
                values,
                width,
            )
        )

    if adds:
        lines.extend(
            _format_section(pal, f"SAFE ADDS  ({len(adds)})", pal.ADD, adds, width)
        )

    if result.failed_parts:
        lines.append(pal(pal.OVERWRITE, f"NOT QUERIED  ({len(result.failed_parts)})"))
        lines.append("  The provider could not be asked about these. This is not the")
        lines.append("  same as an unstocked part, and re-running may well succeed.")
        lines.append("")
        for mpn, reason in result.failed_parts:
            lines.append(f"  {pal(pal.OVERWRITE, 'x')} {mpn}")
            lines.append(f"      {pal(pal.DIM, reason)}")
        lines.append("")

    if result.missing_parts:
        lines.append(pal(pal.WARN, f"UNRESOLVED  ({len(result.missing_parts)})"))
        lines.append("  These part numbers did not resolve. The library is unchanged")
        lines.append("  for them; check the MPN, or the part may simply be unstocked.")
        lines.append("")
        for mpn in result.missing_parts:
            lines.append(f"  {pal(pal.WARN, '!')} {mpn}")
        lines.append("")

    if not changes and not result.missing_parts and not result.failed_parts:
        lines.append("  (nothing to do - already up to date)")
        lines.append("")

    lines.append(pal(pal.HEADING, "SUMMARY"))
    lines.append(f"  written     {'yes' if result.written else 'no'}")
    if result.lookups:
        lines.append(f"  {result.lookups}")
    if sel.is_active():
        lines.append(f"  filters     {sel.describe()}")
        held = sel.suppressed_fields()
        if held:
            lines.append(
                pal(
                    pal.DIM,
                    f"  held back   {', '.join(held)}  (excluded by the filters above)",
                )
            )
    return "\n".join(lines) + "\n"
