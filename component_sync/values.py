"""Derive a symbol's display ``Value`` from distributor parameters.

KiCad's ``Value`` field is a human-readable label, not the part number. For a
capacitor it reads ``100 pF``, for a resistor ``10K``, for an inductor ``560 nH``
and for a crystal ``16 MHz``. Those labels are exactly the quantities a
distributor publishes as *parameters*, so they can be derived rather than
guessed.

The derivation is type-driven: whichever parameter is present decides the type
and therefore the formatting. Components that expose none of them (ICs,
connectors, switches) are left alone, because their ``Value`` is conventionally
the part number.

This module only produces a value when it can do so with confidence. When no
recognisable quantity is present the caller writes nothing.
"""

from __future__ import annotations

import math
import re

__all__ = ["ComponentType", "derive_value", "classify"]

#: Component families this module can label.
ComponentType = str

_CAPACITANCE = "capacitance"
_RESISTANCE = "resistance"
_INDUCTANCE = "inductance"
_FREQUENCY = "frequency"
_UNKNOWN = "unknown"

#: Parameter aliases, normalised the same way the provider normalises keys.
_TYPE_KEYS: dict[ComponentType, tuple[str, ...]] = {
    _CAPACITANCE: ("capacitance", "capacitance_ Tolerance".split()[0], "rated_capacitance"),
    _RESISTANCE: ("resistance", "resistance_value"),
    _INDUCTANCE: ("inductance", "inductance_value"),
    _FREQUENCY: ("frequency", "frequency_stability", "cutoff_frequency", "load_capacitance"),
}

#: Quantity suffix letters, longest first so "pF" beats "F".
_SUFFIXES: tuple[tuple[float, str], ...] = (
    (1e12, "T"),
    (1e9, "G"),
    (1e6, "M"),
    (1e3, "k"),
    (1.0, ""),
    (1e-3, "m"),
    (1e-6, "u"),
    (1e-9, "n"),
    (1e-12, "p"),
)

_MULTIPLIERS: dict[ComponentType, str] = {
    _CAPACITANCE: "F",
    _RESISTANCE: "ohm",
    _INDUCTANCE: "H",
    _FREQUENCY: "Hz",
}

_NUM = r"([+-]?\d+(?:\.\d+)?)\s*([a-zA-ZµμΩ]+)"


def _normalise(text: str) -> str:
    """Return a comparison key for a parameter name.

    Args:
        text: The raw parameter name.

    Returns:
        Lowercase text with spaces, dashes and underscores unified.
    """
    return text.strip().casefold().replace(" ", "_").replace("-", "_")


def classify(parameters: dict[str, str]) -> ComponentType:
    """Determine the component family from its parameters.

    Args:
        parameters: Vendor parameters keyed by name.

    Returns:
        One of the ``ComponentType`` constants, or ``"unknown"``.
    """
    lookup = {_normalise(key): value for key, value in parameters.items()}
    for kind in (_CAPACITANCE, _RESISTANCE, _INDUCTANCE, _FREQUENCY):
        for alias in _TYPE_KEYS[kind]:
            if lookup.get(alias, "").strip():
                return kind
    return _UNKNOWN


def _parse_number(text: str) -> tuple[float, str] | None:
    """Parse the first number and its optional suffix from a value string.

    Args:
        text: A parameter value such as ``"0.1 uF"`` or ``"10 kOhms"``.

    Returns:
        A ``(magnitude, suffix)`` pair, or ``None`` when no number is present.
    """
    match = re.search(_NUM, text)
    if match is None:
        return None
    try:
        magnitude = float(match.group(1))
    except ValueError:  # pragma: no cover - regex guarantees a number
        return None
    return magnitude, match.group(2).strip()


#: Unit names that take no prefix, so "mOhm" is milli-ohm and not mega-ohm.
_BASE_UNITS = frozenset({"ohm", "ohms", "hertz", "farad", "f", "henry", "h"})


def _si_scale(suffix: str, multipliers: dict[str, str]) -> float:
    """Return the multiplier implied by a unit suffix.

    Case is significant and is preserved from the source: ``"MOhm"`` is
    mega-ohm while ``"mOhm"`` is milli-ohm. Compound units are resolved
    longest-prefix-first so ``"kOhms"`` reads as kilo.

    Args:
        suffix: The suffix exactly as written, for example ``"kOhms"``.
        multipliers: Mapping of component kind to base unit name.

    Returns:
        The multiplier to apply; ``1.0`` when the suffix is unrecognised.
    """
    if not suffix:
        return 1.0

    units = set(multipliers.values()) | set(_BASE_UNITS)
    for unit in units:
        if suffix.casefold() == unit.casefold():
            return 1.0

    # Longest prefix wins, matched case-sensitively so "M" != "m".
    for scale, prefix in sorted(_SUFFIXES, key=lambda item: -len(item[1])):
        if not prefix:
            continue
        if suffix.startswith(prefix):
            return scale
    return 1.0




def _render(value: float, unit: str, *, resistance_style: bool) -> str:
    """Render a magnitude with an SI prefix and unit.

    Resistors use KiCad's compact schematic notation, where the prefix follows
    the digits: ``10K``, ``4K7``, ``2R2``, ``0R``.

    Args:
        value: The raw magnitude, in base units.
        unit: The base unit for the quantity, for example ``"F"``.
        resistance_style: When true, render as a resistance rather than as a
            quantity with a unit.

    Returns:
        The formatted label, or ``""`` for a non-finite magnitude.
    """
    if not math.isfinite(value):
        return ""
    if value == 0:
        return "0R" if resistance_style else f"0 {unit}"

    magnitude = abs(value)
    scale = 1.0
    prefix = ""
    for candidate, candidate_prefix in _SUFFIXES:
        if magnitude >= candidate:
            scale = candidate
            prefix = candidate_prefix
            break
    else:  # pragma: no cover - _SUFFIXES reaches 1e-12
        scale = 1e-12
        prefix = "p"

    scaled = value / scale

    if resistance_style:
        return _render_resistance(scaled, prefix)

    rounded = round(scaled, 6)
    digits = str(int(rounded)) if rounded == int(rounded) else f"{rounded:g}"
    return f"{digits} {prefix}{unit}"


#: SI prefixes rendered in the case KiCad's schematic notation expects.
_RESISTANCE_PREFIX_CASE = {
    "k": "K",
    "M": "M",
    "G": "G",
    "T": "T",
    "m": "m",
    "u": "R",
    "n": "R",
    "p": "R",
}


def _render_resistance(scaled: float, prefix: str) -> str:
    """Render a resistance in KiCad's compact notation.

    Whole multiples become ``10K``, ``1M``. Fractional ones use the digit
    substitution form ``4K7``, ``2R2``, and sub-unit values keep an explicit
    prefix such as ``100m``.

    Args:
        scaled: The magnitude after SI scaling.
        prefix: The SI prefix that was removed, for example ``"k"``.

    Returns:
        A label such as ``10K``, ``4K7``, ``2R2`` or ``100m``.
    """
    suffix = _RESISTANCE_PREFIX_CASE.get(prefix, prefix)
    text = f"{scaled:g}"

    if "." not in text:
        return f"{text}{suffix}"

    head, _, tail = text.partition(".")

    if prefix in ("", "m", "u", "n", "p"):
        # Sub-unit and base-unit fractions use R as the decimal point:
        # 1.5 Ohm -> 1R5, 4.7u -> 4R7, 2.2n -> 2R2.
        return f"{head}R{tail}"

    # Larger prefixes stay attached to the magnitude: 4.7k -> 4K7, 1.5M -> 1M5.
    return f"{head}{suffix}{tail}"


def derive_value(parameters: dict[str, str], *, current: str = "") -> str:
    """Derive a display ``Value`` for a component from vendor parameters.

    Args:
        parameters: Vendor parameters keyed by name.
        current: The existing ``Value``, used only to preserve an already
            correct label when the derived form differs cosmetically.

    Returns:
        The derived label, or ``""`` when nothing could be derived with
        confidence. Callers must leave the field untouched in that case.
    """
    kind = classify(parameters)
    if kind == _UNKNOWN:
        return ""

    lookup = {_normalise(key): value for key, value in parameters.items()}
    raw = ""
    for alias in _TYPE_KEYS[kind]:
        candidate = lookup.get(alias, "").strip()
        if candidate:
            raw = candidate
            break
    if not raw:
        return ""

    parsed = _parse_number(raw)
    if parsed is None:
        return ""

    magnitude, suffix = parsed
    scale = _si_scale(suffix, _MULTIPLIERS)
    base = magnitude * scale
    if not math.isfinite(base):
        return ""
    if base == 0 and kind != _RESISTANCE:
        # A zero capacitance or inductance carries no useful label; a zero-ohm
        # resistor is a real, orderable part.
        return ""

    return _render(
        base,
        _MULTIPLIERS[kind],
        resistance_style=(kind == _RESISTANCE),
    )
