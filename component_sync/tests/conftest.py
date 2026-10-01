"""Shared pytest fixtures for :mod:`component_sync` tests."""

from __future__ import annotations

from typing import Any

import pytest

from component_sync.models import ComponentData
from component_sync.providers.base import BaseProvider


class StubProvider(BaseProvider):
    """Deterministic provider used instead of real HTTP in tests.

    Attributes:
        name: Registry key for the stub.
        calls: Every MPN passed to :meth:`fetch_component_data`, in order.
        catalogue: MPN to record lookup, or ``None`` to simulate a miss.
    """

    name = "stub"

    def __init__(self, catalogue: dict[str, ComponentData]) -> None:
        """Initialise the stub.

        Args:
            catalogue: Mapping of MPN to the record that should be returned.
        """
        self.catalogue = catalogue
        self.calls: list[str] = []

    def authenticate(self) -> None:
        """No-op: the stub needs no credentials."""

    def fetch_component_data(self, mpn: str) -> ComponentData:
        """Return the catalogue entry for ``mpn``.

        Args:
            mpn: Part number to resolve.

        Returns:
            The matching record.

        Raises:
            PartNotFoundError: If the MPN is absent from the catalogue.
        """
        from component_sync.exceptions import PartNotFoundError

        self.calls.append(mpn)
        try:
            return self.catalogue[mpn]
        except KeyError as exc:
            raise PartNotFoundError(mpn) from exc


@pytest.fixture
def sample_component() -> ComponentData:
    """Return a representative component record.

    Returns:
        A populated :class:`ComponentData`.
    """
    return ComponentData(
        mpn="GRM155R61C104KA88D",
        manufacturer="Murata",
        description="Multilayer Ceramic Capacitors MLCC",
        voltage="16 VDC",
        operating_temp="-55 C / +85 C",
        package="0402",
        raw_parameters={"Voltage Rating": "16 VDC"},
    )


@pytest.fixture
def stub_provider(sample_component: ComponentData) -> StubProvider:
    """Return a stub provider holding one known part.

    Args:
        sample_component: The record the stub should serve.

    Returns:
        A configured stub provider.
    """
    return StubProvider({sample_component.mpn: sample_component})


@pytest.fixture
def kicad_sym_text() -> str:
    """Return a minimal but realistic KiCad 8 symbol library.

    The text is deliberately formatted with tabs and a comment so tests can
    prove that formatting outside the edited spans is preserved.

    Returns:
        The library source text.
    """
    return (
        "(kicad_symbol_lib\n"
        "\t(version 20231120)\n"
        "\t(generator \"component_sync\")\n"
        "\t; hand maintained library\n"
        "\t(symbol \"TESTCAP\"\n"
        "\t\t(exclude_from_sim no)\n"
        "\t\t(in_bom yes)\n"
        "\t\t(property \"Reference\" \"C\"\n"
        "\t\t\t(at 0 0 0)\n"
        "\t\t\t(show_name no)\n"
        "\t\t)\n"
        "\t\t(property \"Value\" \"100nF\"\n"
        "\t\t\t(at 0 0 0)\n"
        "\t\t\t(show_name no)\n"
        "\t\t)\n"
        "\t\t(property \"Footprint\" \"fp:C_0402\"\n"
        "\t\t\t(at 0 0 0)\n"
        "\t\t)\n"
        "\t\t(property \"Part\" \"GRM155R61C104KA88D\"\n"
        "\t\t\t(at 0 0 0)\n"
        "\t\t)\n"
        "\t\t(property \"Voltage\" \"0 VDC\"\n"
        "\t\t\t(at 0 0 0)\n"
        "\t\t)\n"
        "\t\t(embedded_fonts no)\n"
        "\t)\n"
        ")\n"
    )


@pytest.fixture
def csv_bom_text() -> str:
    """Return a small CSV bill of materials.

    Returns:
        The BOM source text.
    """
    return (
        '"Reference","Value","Part"\n'
        '"C1","100nF","GRM155R61C104KA88D"\n'
        '"R1","10K","RC0402FR-0710KL"\n'
    )


def write(path: Any, text: str) -> Any:
    """Write ``text`` to ``path`` and return the path.

    Args:
        path: Destination path.
        text: Contents to write.

    Returns:
        The path that was written.
    """
    path.write_text(text, encoding="utf-8")
    return path
