"""One-off repair: add (hide yes) to properties this tool added without it.

The first write run inserted ``Digikey``, ``Voltage Rating`` and some ``Package``
properties with no visibility flag, so KiCad renders their text on the schematic.
This restores the author's intent without touching any value.

Run:  python -m component_sync.fix_visibility <library.kicad_sym> [--dry-run]
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from .processors.base import atomic_write

#: A property block, capturing its name and body up to the closing paren.
_BLOCK = re.compile(r'\(property "([^"]+)" "([^"]*)"\n(.*?)\n\t\t\)', re.S)
#: The exact place KiCad writes the flag: after do_not_autoplace, before effects.
_ANCHOR = "\t\t\t(do_not_autoplace no)\n"
_HIDE = "\t\t\t(hide yes)\n"

#: Only these names are candidates. Anything else was written by hand.
MANAGED = (
    "Digikey", "Package", "Voltage Rating", "Voltage Min", "Voltage Max",
    "Voltage Min", "Temperature Min", "Temperature Max", "Operating Temperature",
)


def repair(text: str) -> tuple[str, int]:
    """Return the text with visibility restored, and how many blocks changed.

    Args:
        text: The full library source.

    Returns:
        A ``(new_text, count)`` pair.
    """
    count = 0

    def fix(match: re.Match[str]) -> str:
        nonlocal count
        name, value, body = match.group(1), match.group(2), match.group(3)
        if name not in MANAGED or "(hide yes)" in body or _ANCHOR not in body:
            return match.group(0)
        count += 1
        return f'\t\t(property "{name}" "{value}"\n' + body.replace(
            _ANCHOR, _ANCHOR + _HIDE, 1
        ) + "\n\t\t)"

    return _BLOCK.sub(fix, text), count


def main(argv: list[str] | None = None) -> int:
    """Add ``(hide yes)`` to managed properties that lack it.

    Args:
        argv: Argument vector, defaulting to :data:`sys.argv`.

    Returns:
        Process exit status.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("library", type=Path, help="Path to a .kicad_sym file.")
    parser.add_argument(
        "-n", "--dry-run", action="store_true", help="Report without writing."
    )
    args = parser.parse_args(argv)

    original = args.library.read_text(encoding="utf-8")
    updated, count = repair(original)
    print(f"{args.library}: {count} propert{'y' if count == 1 else 'ies'} would be hidden")
    if args.dry_run or count == 0:
        return 0
    atomic_write(args.library, updated)
    print("written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
