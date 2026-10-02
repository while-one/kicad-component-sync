"""Tests for vendor range-string parsing and canonical bound formatting."""

from __future__ import annotations

import pytest

from component_sync.models import ComponentData
from component_sync.ranges import parse_range


class TestParseRange:
    """Verify extraction of lower and upper bounds."""

    @pytest.mark.parametrize(
        ("text", "low", "high", "unit"),
        [
            ("-55/+150 C", -55.0, 150.0, "C"),
            ("-55 C / +85 C", -55.0, 85.0, "C"),
            ("-40C to +150C", -40.0, 150.0, "C"),
            ("-55/+125", -55.0, 125.0, ""),
            ("2.65 V to 3.6 V", 2.65, 3.6, "V"),
            ("0.5 V ~ 3.6 V", 0.5, 3.6, "V"),
            ("1.8V-5.5V", 1.8, 5.5, "V"),
        ],
    )
    def test_two_value_ranges(self, text: str, low: float, high: float, unit: str) -> None:
        """Both bounds are extracted from the common range spellings."""
        bounds = parse_range(text)
        assert bounds is not None
        assert bounds.low == low
        assert bounds.high == high
        assert bounds.unit == unit
        assert bounds.has_bounds is True

    def test_inverted_range_is_normalised(self) -> None:
        """A reversed range is reordered rather than rejected."""
        bounds = parse_range("+150/-55 C")
        assert bounds is not None
        assert bounds.low == -55.0
        assert bounds.high == 150.0

    def test_bracketed_fragment_in_description(self) -> None:
        """A range inside a longer description is still found."""
        bounds = parse_range("Battery Holders, CR2032, Power, -40 257 F [-40 125 C]")
        assert bounds is not None
        assert bounds.low == -40.0
        assert bounds.high == 125.0
        assert bounds.is_temperature is True

    def test_single_voltage_has_no_upper_bound(self) -> None:
        """A single value yields one bound, never an invented second one."""
        bounds = parse_range("16 VDC")
        assert bounds is not None
        assert bounds.low == 16.0
        assert bounds.high is None
        assert bounds.has_bounds is False

    def test_single_temperature_treated_as_maximum(self) -> None:
        """A bare positive temperature is conventionally the upper limit."""
        bounds = parse_range("85 C")
        assert bounds is not None
        assert bounds.low is None
        assert bounds.high == 85.0

    def test_temperature_units_detected(self) -> None:
        """Celsius and Fahrenheit are both recognised as temperatures."""
        celsius = parse_range("-55/+125 C")
        fahrenheit = parse_range("-40 ~ 257 F")
        assert celsius is not None and celsius.is_temperature is True
        assert fahrenheit is not None and fahrenheit.is_temperature is True

    @pytest.mark.parametrize("text", ["", "   ", "not applicable", "16 VDC +/- 10%"])
    def test_unparseable_returns_none(self, text: str) -> None:
        """Text without two usable values yields no bounds."""
        assert parse_range(text) is None

    def test_text_is_preserved(self) -> None:
        """The original string is retained for verbatim use."""
        bounds = parse_range("16 VDC")
        assert bounds is not None
        assert bounds.text == "16 VDC"


class TestBoundFormatting:
    """Verify canonical bound rendering."""

    @pytest.mark.parametrize(
        ("value", "temperature", "expected"),
        [
            (-55.0, True, "-55 C"),
            (150.0, True, "+150 C"),
            (0.0, True, "+0 C"),
            (-40.0, False, "40 V"),
            (2.65, False, "2.65 V"),
            (5.0, False, "5 V"),
        ],
    )
    def test_formatting(self, value: float, temperature: bool, expected: str) -> None:
        """Bounds render with a single sign and a space before the unit."""
        unit = "C" if temperature else "V"
        assert ComponentData._fmt_bound(value, unit, temperature=temperature) == expected

    def test_negative_never_doubles_the_sign(self) -> None:
        """Regression: a negative bound must not render as ``--55 C``."""
        assert ComponentData._fmt_bound(-55.0, "C", temperature=True) == "-55 C"

    def test_non_finite_is_rejected(self) -> None:
        """NaN and infinity produce no bound rather than a broken string."""
        assert ComponentData._fmt_bound(float("nan"), "C", temperature=True) == ""
        assert ComponentData._fmt_bound(float("inf"), "C", temperature=True) == ""


class TestPropertyMapping:
    """Verify the Min/Max field names produced for each shape."""

    def test_range_becomes_min_and_max(self) -> None:
        """A temperature range writes Temperature Min and Temperature Max."""
        data = ComponentData(mpn="X", temp_min="-55 C", temp_max="+85 C", package="0402")
        properties = data.as_properties()
        assert properties["Temperature Min"] == "-55 C"
        assert properties["Temperature Max"] == "+85 C"
        assert "Operating Temperature" not in properties

    def test_voltage_range_uses_min_max(self) -> None:
        """A voltage range writes Voltage Min and Voltage Max."""
        data = ComponentData(mpn="X", voltage_min="2.65 V", voltage_max="3.5 V")
        properties = data.as_properties()
        assert properties["Voltage Min"] == "2.65 V"
        assert properties["Voltage Max"] == "3.5 V"
        assert "Voltage" not in properties

    def test_single_value_falls_back_to_rating(self) -> None:
        """A lone voltage is preserved as Voltage Rating, not split."""
        data = ComponentData(mpn="X", voltage_text="16 VDC")
        assert data.as_properties() == {"Voltage Rating": "16 VDC"}

    def test_single_temperature_falls_back_to_verbatim(self) -> None:
        """A lone temperature keeps the original string."""
        data = ComponentData(mpn="X", temp_text="85 C")
        assert data.as_properties() == {"Operating Temperature": "85 C"}

    def test_empty_fields_are_omitted(self) -> None:
        """Absent values never produce blank placeholders."""
        data = ComponentData(mpn="X", manufacturer="Murata")
        assert data.as_properties() == {"Manufacturer": "Murata"}


class TestScientificNotation:
    """An exponent belongs to the number, not to the next one.

    Without exponent support in the number pattern, the loose scan found two
    numbers inside ``1e400`` -- ``1`` and ``400`` -- and reported a range of 1 to
    400. ``1.8e3 V ~ 2.2e3 V`` became 1.8 to 3.0 V. Both looked plausible and
    both would have been written to ``Voltage Min``/``Max``.
    """

    def test_orphaned_exponent_is_not_two_numbers(self) -> None:
        """A malformed number yields nothing rather than a spurious range."""
        assert parse_range("1e400") is None

    def test_real_exponents_are_understood(self) -> None:
        """A genuine scientific-notation range is read correctly."""
        bounds = parse_range("1.8e3 V ~ 2.2e3 V")
        assert bounds is not None
        assert bounds.low == 1800.0
        assert bounds.high == 2200.0
        assert bounds.unit == "V"

    @pytest.mark.parametrize(
        ("text", "low", "high"),
        [
            ("1e3 ~ 2e3 V", 1000.0, 2000.0),
            ("-40e0 C ~ 85e0 C", -40.0, 85.0),
            ("1.5e-3 V ~ 2.5e-3 V", 0.0015, 0.0025),
        ],
    )
    def test_exponent_forms(self, text: str, low: float, high: float) -> None:
        """Positive, negative and fractional exponents all work."""
        bounds = parse_range(text)
        assert bounds is not None
        assert bounds.low == pytest.approx(low)
        assert bounds.high == pytest.approx(high)

    def test_overflowing_literal_yields_nothing(self) -> None:
        """A bound of infinity is not a limit."""
        assert parse_range("1e400 V ~ 2e400 V") is None
        assert parse_range("1e400 V") is None

    def test_exponent_does_not_leak_into_a_neighbouring_parse(self) -> None:
        """One bad token must not poison the rest of a string."""
        bounds = parse_range("[1e400 5 V]")
        assert bounds is None or bounds.high == 5.0

    @pytest.mark.parametrize(
        "text",
        ["-55°C ~ 85°C", "1.8V ~ 5.5V", "1.65 V ~ 5.5 V", "16V", "6.3 V", "50 VDC"],
    )
    def test_ordinary_ranges_are_unchanged(self, text: str) -> None:
        """The fix must not disturb strings that were already correct."""
        assert parse_range(text) is not None
