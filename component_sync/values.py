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

#: Base unit for each component family.
#:
#: Resistance is written ``kOhm`` rather than a bare ``K`` so that every
#: quantity in the library takes the same shape: digits, space, prefixed unit.
_MULTIPLIERS: dict[ComponentType, str] = {
    _CAPACITANCE: "F",
    _RESISTANCE: "Ohm",
    _INDUCTANCE: "H",
    _FREQUENCY: "Hz",
}

#: A numeric literal, optionally in scientific notation, followed by a unit.
#:
#: The exponent must be consumed as part of the number: treating ``1e3`` as the
#: number ``1`` with the unit ``e3`` would silently misread it as 1 farad.
#:
#: A single-letter unit is only accepted when it is not a bare ``e``, which is
#: what separates the valid unit ``1 F`` from the incomplete number ``1e400``.
_NUM = r"([+-]?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)\s*([a-zA-ZµμΩ]+)"

#: Suffixes that are really an orphaned exponent marker rather than a unit.
#:
#: ``1e400`` is a malformed number, not 1 with the unit "e". Rejecting it keeps
#: a typo from becoming a plausible-looking value such as 400 V.
_EXPONENT_LOOKALIKES = frozenset({"e", "E"})


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

    A trailing ``e`` that is not part of an exponent is rejected. Without this,
    the malformed input ``1e400`` parses as the number ``1`` with the unit
    ``e``, and a capacitor would be labelled 1 F.

    Args:
        text: A parameter value such as ``"0.1 uF"`` or ``"10 kOhms"``.

    Returns:
        A ``(magnitude, suffix)`` pair, or ``None`` when no number is present.
    """
    match = re.search(_NUM, text)
    if match is None:
        return None
    suffix = match.group(2).strip()
    if suffix.casefold() in _EXPONENT_LOOKALIKES:
        return None
    try:
        magnitude = float(match.group(1))
    except ValueError:  # pragma: no cover - regex guarantees a number
        return None
    if not math.isfinite(magnitude):
        return None
    return magnitude, suffix


#: Unit names that take no prefix, so "mOhm" is milli-ohm and not mega-ohm.
_BASE_UNITS = frozenset({"ohm", "ohms", "hertz", "farad", "f", "henry", "h"})


#: Characters a distributor may use for the micro prefix.
#:
#: DigiKey sends the micro sign (U+00B5), the Greek small letter mu (U+03BC) is
#: also in circulation, and hand-written BOMs use a plain ``u``. All three mean
#: the same thing, and treating them as different would misread ``0.1 µF`` as
#: 0.1 farads, which then renders as the plausible-looking ``100 mF`` instead of
#: the correct ``100 nF``. Input is therefore folded onto ASCII ``u``; output
#: stays ASCII too, so the library contains no exotic characters.
_MICRO_CHARACTERS = ("\u00b5", "\u03bc")


def _fold_micro(text: str) -> str:
    """Replace every micro-prefix character with a plain ASCII ``u``.

    Args:
        text: A unit suffix as written by a distributor or a human.

    Returns:
        The suffix with all micro characters replaced by ``u``.
    """
    for character in _MICRO_CHARACTERS:
        text = text.replace(character, "u")
    return text


def _si_scale(suffix: str, multipliers: dict[str, str]) -> float:
    """Return the multiplier implied by a unit suffix.

    Case is significant and is preserved from the source: ``"MOhm"`` is
    mega-ohm while ``"mOhm"`` is milli-ohm. Compound units are resolved
    longest-prefix-first so ``"kOhms"`` reads as kilo.

    The micro prefix is matched as ``u``, ``µ`` or ``μ``, because vendors do not
    agree on which character to send.

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

    folded = _fold_micro(suffix)

    # Longest prefix wins, matched case-sensitively so "M" != "m".
    for scale, prefix in sorted(_SUFFIXES, key=lambda item: -len(item[1])):
        if not prefix:
            continue
        if folded.startswith(prefix):
            return scale
    return 1.0




def _significant_digits(value: float, digits: int = 9) -> str:
    """Render a magnitude without trailing zeros or exponent notation.

    Nine significant digits is far more than any real component value needs,
    so the result keeps every digit the vendor supplied: 27.12 MHz stays
    27.12 rather than being rounded to 27.1.

    Args:
        value: The magnitude to render.
        digits: Maximum number of significant digits to keep.

    Returns:
        A plain decimal string, for example ``"4.7"`` or ``"27.12"``.
    """
    text = f"{value:.{digits}g}"
    if "e" in text or "E" in text:  # pragma: no cover - scaled values stay small
        text = f"{value:f}".rstrip("0").rstrip(".")
    return text


def _render(value: float, unit: str) -> str:
    """Render a magnitude with an SI prefix and an explicit unit.

    Every quantity uses the same form: digits, a space, then the prefixed unit.
    ``k`` is written as ``kOhm`` rather than a single ``K`` so a kilo-ohm is
    never confused with a bare symbol, and so resistance reads the same way as
    capacitance.

    The largest SI prefix that keeps the magnitude at or above one is used, so
    1000 pF becomes ``1 nF`` and 1000 nF becomes ``1 uF``.

    Args:
        value: The raw magnitude, in base units.
        unit: The base unit for the quantity, for example ``"F"``.

    Returns:
        The formatted label, or ``""`` for a non-finite magnitude.
    """
    if not math.isfinite(value):
        return ""
    if value == 0:
        return f"0 {unit}"

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

    digits = _significant_digits(round(value / scale, 9))
    return f"{digits} {prefix}{unit}"





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

    return _render(base, _MULTIPLIERS[kind])
