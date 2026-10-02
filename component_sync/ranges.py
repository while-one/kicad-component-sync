"""Parse vendor range strings into discrete lower and upper bounds.

Distributors express a limit in many ways. The same 16 V capacitor appears as
``"16 VDC"``, ``"16V"``, ``"16 V"`` or ``"50 VDC"``, and a temperature appears as
``"-55/+150 C"``, ``"-55 C / +85 C"``, ``"-40 ~ 85"`` or a bare ``"85 C"``. This
module turns those into a ``(low, high, unit)`` triple so the rest of the package
can write canonical ``Min``/``Max`` fields.

Parsing never invents a bound. When only one value is present only one bound is
produced, and the caller decides how to record it.
"""

from __future__ import annotations

import math
import re

__all__ = ["RangeBounds", "parse_range"]

#: Numeric literal with optional sign, decimal point and exponent.
#:
#: The exponent must be consumed as part of the number. Without it the loose
#: scan found two numbers in ``1e400`` -- ``1`` and ``400`` -- and reported a
#: range of 1 to 400, while ``1.8e3 V ~ 2.2e3 V`` became 1.8 to 3.0 V. Both are
#: plausible-looking and completely wrong, and both feed ``Voltage Min``/``Max``
#: and ``Temperature Min``/``Max`` in the symbol library.
_NUMBER = r"[+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?"

#: Characters vendors use as range separators, ordered so the longest match first.
_SEPARATORS = r"(?:\s*(?:/|~|-|–|—|\bto\b|\bthrough\b|\.\.\.)\s*)"

#: Unit suffixes, longest alternative first so "VDC" beats "V".
_UNITS = (
    r"VDC|VAC|V|VDC\b|°C|°F|degC|C|F|celsius|fahrenheit|"
    r"mV|kV|µV|uV|mA|A|W|mW|kW|Hz|kHz|MHz|GHz|ohm|Ω|Ohm"
)

_RANGE_RE = re.compile(
    rf"^(?P<low>{_NUMBER})\s*(?P<lunit>{_UNITS})?"
    rf"{_SEPARATORS}"
    rf"(?P<high>{_NUMBER})\s*(?P<hunit>{_UNITS})?$",
    re.IGNORECASE,
)

_SINGLE_RE = re.compile(
    rf"^(?P<value>{_NUMBER})\s*(?P<unit>{_UNITS})?$",
    re.IGNORECASE,
)

#: Numeric values anywhere in a string, used as a last resort.
_ANY_NUMBER_RE = re.compile(_NUMBER)

_TEMPERATURE_UNITS = {"c", "°c", "f", "°f", "degc", "celsius", "fahrenheit"}


class RangeBounds:
    """Lower and upper bounds extracted from a vendor string.

    Attributes:
        low: Lower bound, or ``None`` when absent.
        high: Upper bound, or ``None`` when absent.
        unit: Normalised unit suffix, for example ``"C"`` or ``"V"``.
        text: The original string, preserved verbatim.
        is_temperature: Whether the unit indicates a temperature.
    """

    __slots__ = ("low", "high", "unit", "text", "is_temperature")

    def __init__(
        self,
        low: float | None,
        high: float | None,
        unit: str,
        text: str,
        is_temperature: bool,
    ) -> None:
        """Initialise the bounds.

        Args:
            low: Lower bound, or ``None``.
            high: Upper bound, or ``None``.
            unit: Normalised unit suffix.
            text: The original vendor string.
            is_temperature: Whether the unit indicates a temperature.
        """
        self.low = low
        self.high = high
        self.unit = unit
        self.text = text
        self.is_temperature = is_temperature

    @property
    def has_bounds(self) -> bool:
        """Whether both a lower and an upper bound were found."""
        return self.low is not None and self.high is not None

    def __repr__(self) -> str:
        """Return a debug representation."""
        return (
            f"RangeBounds(low={self.low!r}, high={self.high!r}, "
            f"unit={self.unit!r}, is_temperature={self.is_temperature!r})"
        )


def _normalise_unit(unit: str | None) -> str:
    """Return a canonical unit suffix.

    Args:
        unit: The raw unit as written by the vendor.

    Returns:
        ``"C"``, ``"F"``, ``"V"``, ``"W"``, ``"A"``, ``"Hz"``, ``"ohm"`` or
        ``""`` when the unit was absent or unrecognised.
    """
    if not unit:
        return ""
    token = unit.strip().lower()
    if token in _TEMPERATURE_UNITS:
        return "F" if "f" in token and "c" not in token else "C"
    if token in ("v", "vdc", "vac", "volt", "volts"):
        return "V"
    if token in ("w", "mw", "kw", "watt", "watts"):
        return "W"
    if token in ("a", "ma", "amp", "amps", "ampere"):
        return "A"
    if token in ("hz", "khz", "mhz", "ghz"):
        return "Hz"
    if token in ("ohm", "ohms", "ω"):
        return "ohm"
    return ""


def parse_range(text: str) -> RangeBounds | None:
    """Parse a vendor limit string into lower and upper bounds.

    Handles a single value (``"16 VDC"``), an explicit range in any of the
    common separator styles (``"-55/+150 C"``, ``"-55 C / +85 C"``,
    ``"2.65 V to 3.6 V"``), and parenthesised range fragments found inside longer
    descriptions.

    Args:
        text: The vendor string to parse.

    Returns:
        The extracted bounds, or ``None`` when no numeric value is present.
    """
    if not text or not text.strip():
        return None

    raw = text.strip()
    unit = ""
    is_temperature = False

    # 1. Whole string is a clean range.
    match = _RANGE_RE.match(raw)
    if match:
        low, high = float(match.group("low")), float(match.group("high"))
        if not (is_finite(low) and is_finite(high)):
            # An overflowing literal such as 1e400 parses as infinity. A bound
            # of infinity is not a limit, so nothing is reported rather than
            # something that looks like a real maximum.
            return None
        unit = _normalise_unit(match.group("hunit") or match.group("lunit"))
        is_temperature = unit in ("C", "F")
        if low > high:
            low, high = high, low
        return RangeBounds(low, high, unit, raw, is_temperature)

    # 2. Whole string is a single value.
    match = _SINGLE_RE.match(raw)
    if match:
        value = float(match.group("value"))
        if not is_finite(value):
            return None
        unit = _normalise_unit(match.group("unit"))
        is_temperature = unit in ("C", "F")
        # A bare "125 C" is ambiguous: it is conventionally the upper limit.
        if is_temperature and value > 0:
            return RangeBounds(None, value, unit, raw, is_temperature)
        return RangeBounds(value, None, unit, raw, is_temperature)

    # 3. Look for a parenthesised or bracketed range inside a description.
    fragment = _search_fragment(raw)
    if fragment is not None:
        unit = _normalise_unit(fragment.unit)
        is_temperature = unit in ("C", "F")
        if fragment.low is not None and fragment.high is not None:
            if fragment.low > fragment.high:
                fragment.low, fragment.high = fragment.high, fragment.low
            return RangeBounds(fragment.low, fragment.high, unit, raw, is_temperature)
        if fragment.high is not None:
            return RangeBounds(None, fragment.high, unit, raw, is_temperature)
        if fragment.low is not None:
            return RangeBounds(fragment.low, None, unit, raw, is_temperature)

    return None


def _search_fragment(text: str) -> RangeBounds | None:
    """Find and parse a range buried inside a longer description.

    Args:
        text: The description to search.

    Returns:
        The parsed fragment, or ``None`` when nothing usable was found.
    """
    # Prefer bracketed groups such as "[-40 125 C]" or "(2.65 V to 3.6 V)".
    for opener, closer in (("[", "]"), ("(", ")"), ("<", ">")):
        start = text.find(opener)
        if start == -1:
            continue
        end = text.find(closer, start + 1)
        if end == -1:
            continue
        inner = text[start + 1 : end].strip()
        parsed = _parse_loose(inner)
        if parsed is not None:
            return parsed

    # Otherwise fall back to the first plausible two-number fragment.
    numbers = list(_ANY_NUMBER_RE.finditer(text))
    if len(numbers) >= 2:
        low = float(numbers[0].group())
        high = float(numbers[1].group())
        if not (is_finite(low) and is_finite(high)):
            return None
        if low <= high:
            unit = _unit_after(text, numbers[1].end())
            return RangeBounds(low, high, unit, text, unit in ("C", "F"))
    return None


def _parse_loose(text: str) -> RangeBounds | None:
    """Parse a range fragment that may omit its separator.

    Args:
        text: The fragment, for example ``"-40 125 C"``.

    Returns:
        The parsed bounds, or ``None`` when fewer than two values are present.
    """
    numbers = list(_ANY_NUMBER_RE.finditer(text))
    if len(numbers) < 2:
        return None
    low = float(numbers[0].group())
    high = float(numbers[1].group())
    if not (is_finite(low) and is_finite(high)):
        return None
    if low > high:
        low, high = high, low
    unit = _unit_after(text, numbers[1].end())
    return RangeBounds(low, high, unit, text, unit in ("C", "F"))


def _unit_after(text: str, position: int) -> str:
    """Return the normalised unit immediately after a position.

    Args:
        text: The string being scanned.
        position: Index to start scanning from.

    Returns:
        The normalised unit, or ``""`` when none follows.
    """
    tail = text[position : position + 12]
    match = re.match(rf"\s*({_UNITS})\b", tail, re.IGNORECASE)
    if match is None:
        return ""
    return _normalise_unit(match.group(1))


def is_finite(value: float | None) -> bool:
    """Whether ``value`` is a usable finite number.

    Args:
        value: Candidate value.

    Returns:
        ``True`` when the value is neither ``None`` nor infinite/NaN.
    """
    return value is not None and not math.isnan(value) and not math.isinf(value)
