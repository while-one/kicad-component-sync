"""Tests for Value derivation and its single rendering convention.

The project uses SI notation with an explicit unit everywhere: ``100 nF``,
``10 kOhm``, ``560 nH``, ``16 MHz``. There is exactly one rendering path, so
these tests pin the convention rather than exercising option flags.
"""

from __future__ import annotations

import pytest

from component_sync.values import classify, derive_value


class TestDeriveValue:
    """Verify per-type derivation from vendor parameters."""

    @pytest.mark.parametrize(
        ("parameters", "expected"),
        [
            # Capacitance: SI, largest prefix that keeps the magnitude >= 1.
            ({"Capacitance": "0.1 uF"}, "100 nF"),
            ({"Capacitance": "0.001 uF"}, "1 nF"),
            ({"Capacitance": "100 pF"}, "100 pF"),
            ({"Capacitance": "15 pF"}, "15 pF"),
            ({"Capacitance": "4.7 uF"}, "4.7 uF"),
            ({"Capacitance": "1 nF"}, "1 nF"),
            ({"Capacitance": "100 uF"}, "100 uF"),
            ({"Capacitance": "1000 pF"}, "1 nF"),
            ({"Capacitance": "1000 nF"}, "1 uF"),
            # Resistance: SI with an explicit unit, never R-notation.
            ({"Resistance": "10 kOhms"}, "10 kOhm"),
            ({"Resistance": "100 kOhms"}, "100 kOhm"),
            ({"Resistance": "1 MOhm"}, "1 MOhm"),
            ({"Resistance": "0 Ohms"}, "0 Ohm"),
            ({"Resistance": "4.7 kOhms"}, "4.7 kOhm"),
            ({"Resistance": "2.2 kOhms"}, "2.2 kOhm"),
            ({"Resistance": "36 kOhms"}, "36 kOhm"),
            ({"Resistance": "100 mOhm"}, "100 mOhm"),
            ({"Resistance": "1.5 Ohm"}, "1.5 Ohm"),
            # Inductance.
            ({"Inductance": "560 nH"}, "560 nH"),
            ({"Inductance": "4.7 uH"}, "4.7 uH"),
            ({"Inductance": "2.2 uH"}, "2.2 uH"),
            # Frequency.
            ({"Frequency": "16 MHz"}, "16 MHz"),
            ({"Frequency": "27.12 MHz"}, "27.12 MHz"),
        ],
    )
    def test_derivation(self, parameters: dict[str, str], expected: str) -> None:
        """Each family renders in the project's single SI convention."""
        assert derive_value(parameters) == expected

    @pytest.mark.parametrize("micro", ["\u00b5", "\u03bc", "u"])
    def test_micro_prefix_spelling(self, micro: str) -> None:
        """All three spellings of micro are accepted, because vendors differ.

        DigiKey sends the micro sign U+00B5. Recognising only the ASCII ``u``
        made ``0.1 µF`` read as 0.1 farads, which rendered as the plausible
        ``100 mF`` instead of ``100 nF`` -- wrong by a factor of a million, with
        no error raised.
        """
        assert derive_value({"Capacitance": f"0.1 {micro}F"}) == "100 nF"
        assert derive_value({"Inductance": f"4.7 {micro}H"}) == "4.7 uH"
        assert derive_value({"Capacitance": f"100 {micro}F"}) == "100 uF"

    def test_output_never_contains_exotic_characters(self) -> None:
        """Input may be micro sign; output stays plain ASCII.

        Symbol libraries are hand-maintained plain text, and a stray U+00B5 is
        easy to mangle when typing a new value by hand.
        """
        for parameters in (
            {"Capacitance": "0.1 \u00b5F"},
            {"Inductance": "4.7 \u03bcH"},
            {"Resistance": "10 kOhms"},
        ):
            rendered = derive_value(parameters)
            assert rendered.isascii(), rendered

    def test_case_is_significant_in_units(self) -> None:
        """Unit case is preserved: MOhm is mega-ohm, mOhm is milli-ohm."""
        assert derive_value({"Resistance": "1 MOhm"}) == "1 MOhm"
        assert derive_value({"Resistance": "100 mOhm"}) == "100 mOhm"

    @pytest.mark.parametrize(
        "parameters",
        [
            {},
            {"Voltage - Rated": "16 VDC"},
            {"Tolerance": "5 %"},
            {"Description": "no quantity here"},
        ],
    )
    def test_nothing_derived_for_non_passives(self, parameters: dict[str, str]) -> None:
        """ICs, connectors and switches yield no label, so Value is untouched."""
        assert derive_value(parameters) == ""

    def test_zero_capacitance_yields_nothing(self) -> None:
        """A zero capacitance carries no useful label."""
        assert derive_value({"Capacitance": "0 F"}) == ""

    def test_zero_resistance_is_a_real_part(self) -> None:
        """A zero-ohm jumper is orderable and must be labelled."""
        assert derive_value({"Resistance": "0 Ohms"}) == "0 Ohm"

    def test_exponent_notation_is_one_number(self) -> None:
        """``1e3 pF`` is 1000 pF, not the two numbers 1 and 3."""
        assert derive_value({"Capacitance": "1e3 pF"}) == "1 nF"
        assert derive_value({"Resistance": "1.5e2 kOhms"}) == "150 kOhm"

    def test_orphaned_exponent_is_rejected(self) -> None:
        """``1e400`` is malformed, and must not become a plausible value.

        Read carelessly it parses as 1 with the unit "e", which would label a
        capacitor 1 F and a resistor 1 Ohm.
        """
        assert derive_value({"Capacitance": "1e400"}) == ""
        assert derive_value({"Resistance": "1e400"}) == ""
        assert derive_value({"Capacitance": "50 e"}) == ""

    def test_single_letter_units_are_valid(self) -> None:
        """``1 F`` and ``5 V`` are legitimate, and are not exponent artefacts."""
        assert derive_value({"Capacitance": "1 F"}) == "1 F"

    def test_all_output_uses_one_pattern(self) -> None:
        """Every derived label is digits, a space, then a prefixed unit."""
        samples = [
            {"Capacitance": "100 pF"},
            {"Resistance": "10 kOhms"},
            {"Inductance": "560 nH"},
            {"Frequency": "16 MHz"},
        ]
        for parameters in samples:
            rendered = derive_value(parameters)
            digits, _, unit = rendered.partition(" ")
            assert digits, rendered
            assert unit, rendered
            assert not rendered.startswith(" "), rendered
            assert "  " not in rendered, rendered


class TestClassify:
    """Verify type detection."""

    @pytest.mark.parametrize(
        ("parameters", "kind"),
        [
            ({"Capacitance": "1 uF"}, "capacitance"),
            ({"Resistance": "10 kOhms"}, "resistance"),
            ({"Inductance": "1 uH"}, "inductance"),
            ({"Frequency": "16 MHz"}, "frequency"),
            ({"Voltage - Rated": "5 V"}, "unknown"),
            ({}, "unknown"),
        ],
    )
    def test_classification(self, parameters: dict[str, str], kind: str) -> None:
        """The family is decided by whichever parameter is present."""
        assert classify(parameters) == kind

    def test_capacitance_wins_over_voltage(self) -> None:
        """A capacitor that also lists a voltage is still a capacitor."""
        assert classify({"Capacitance": "1 uF", "Voltage - Rated": "50 V"}) == "capacitance"
