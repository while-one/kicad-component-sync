"""KiCad integration for :mod:`component_sync`.

Scope and limitations (important)
---------------------------------
KiCad 10 ships a Python API for the **PCB editor** (``pcbnew``) but **not** for
the Schematic Editor or the Symbol Editor: there is no ``eeschema`` module and
no public API for reading or writing symbols in the open editor. That means the
"edit a symbol in the GUI, press a button, fields appear" workflow described in
the requirements is **not achievable** with a script in KiCad 10.

Two facts verified on this machine (kicad-10.0.6):

* ``import pcbnew`` succeeds, but exposes only PCB-side schematic *parity*
  types (``SCH_SYMBOL_T`` and friends) - not live editor objects.
* ``import eeschema`` fails: no such module.

Consequently this module is a **best-effort bridge**, not a supported plugin. It
attempts to reach the editor through the wx application object and writes the
managed fields if it can. Whether that succeeds depends on KiCad's internal
frame API, which is not stable and was not verified here because it requires an
interactive GUI session.

What actually works today
-------------------------
Run the CLI against the saved ``.kicad_sym`` and let KiCad reload it. That path
is deterministic and is the workflow the README documents:

    $ python -m component_sync.cli mylib.kicad_sym
    $ # in the Symbol Editor: Tools -> Refresh Symbol Libraries

``sync_selected`` is kept because it costs nothing and may work when KiCad
exposes a usable frame, but do not depend on it. If you need a genuinely
one-keystroke in-editor experience, the honest options are a KiCad action plugin
written in C++ against the editor API, or a file watcher that runs the CLI and
then asks KiCad to refresh.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .exceptions import ComponentSyncError
from .models import ComponentData
from .processors.kicad_processor import MANAGED_FIELDS
from .providers.base import BaseProvider
from .providers.factory import ProviderFactory

__all__ = ["sync_selected", "get_selected_mpns", "apply_data_to_symbol"]

LOGGER = logging.getLogger("component_sync.plugin")


def get_selected_mpns() -> list[tuple[Any, str]]:
    """Collect MPNs from the currently selected symbols in the editor.

    Returns:
        A list of ``(symbol, mpn)`` pairs. Symbols without a usable MPN are
        skipped. Returns an empty list when run outside the KiCad editor.
    """
    return _selected_via_wx()


def _selected_via_wx() -> list[tuple[Any, str]]:
    """Resolve the selected schematic symbols through the wx application.

    Returns:
        A list of ``(symbol, mpn)`` pairs; empty when unavailable.
    """
    try:
        import wx
    except Exception:  # noqa: BLE001
        return []

    app = wx.GetApp()
    if app is None:
        return []

    editor = getattr(app, "GetSchematicFrame", lambda: None)()
    if editor is None:
        return []

    collector: list[tuple[Any, str]] = []
    for symbol in _iter_selected(editor):
        mpn = _read_mpn(symbol)
        if mpn:
            collector.append((symbol, mpn))
    return collector


def _iter_selected(editor: Any) -> list[Any]:
    """Return the symbols currently selected in the schematic editor.

    Args:
        editor: The schematic editor frame.

    Returns:
        A list of selected symbol objects.
    """
    for attribute in ("GetSelectedSymbols", "GetSelection"):
        getter = getattr(editor, attribute, None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:  # noqa: BLE001
                continue
    return []


def _read_mpn(symbol: Any) -> str:
    """Return the MPN recorded on a symbol.

    Args:
        symbol: A schematic symbol object.

    Returns:
        The part number, or ``""`` when absent.
    """
    for field_name in ("Part", "MPN", "Manufacturer Part Number"):
        getter = getattr(symbol, "GetField", None)
        if callable(getter):
            try:
                field = getter(field_name)
            except Exception:  # noqa: BLE001
                field = None
            if field is not None:
                value = getattr(field, "GetText", lambda: "")()
                if value and value.strip():
                    return value.strip()
    getter = getattr(symbol, "GetValue", None)
    if callable(getter):
        try:
            value = getter()
        except Exception:  # noqa: BLE001
            value = ""
        if isinstance(value, str):
            return value.strip()
    return ""


def apply_data_to_symbol(symbol: Any, data: ComponentData) -> list[str]:
    """Write managed fields from ``data`` onto ``symbol``.

    Args:
        symbol: A schematic symbol object.
        data: Normalised component data.

    Returns:
        The names of the fields that were written.
    """
    written: list[str] = []
    for name in MANAGED_FIELDS:
        value = data.as_properties().get(name)
        if not value:
            continue
        getter = getattr(symbol, "GetField", None)
        if not callable(getter):
            continue
        try:
            field = getter(name)
            if field is None:
                symbol.AddField(name, value) if hasattr(symbol, "AddField") else None
            else:
                field.SetText(value)
            written.append(name)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("Could not set %s: %s", name, exc)
    return written


def sync_selected(
    provider: BaseProvider | None = None,
    key: str = "digikey",
) -> list[tuple[str, list[str]]]:
    """Resolve and apply parameters for every selected symbol.

    Args:
        provider: Optional pre-built provider; created from the factory when
            omitted.
        key: Provider registry key used when ``provider`` is ``None``.

    Returns:
        One ``(mpn, written_fields)`` pair per symbol that was updated.
    """
    selected = get_selected_mpns()
    if not selected:
        LOGGER.info("No symbols with a part number are selected")
        return []

    active = provider or ProviderFactory.from_env(default=key)
    results: list[tuple[str, list[str]]] = []
    try:
        for symbol, mpn in selected:
            try:
                data = active.fetch_component_data(mpn)
            except ComponentSyncError as exc:
                LOGGER.warning("Skipping %s: %s", mpn, exc)
                continue
            results.append((mpn, apply_data_to_symbol(symbol, data)))
    finally:
        if provider is None:
            active.close()
    return results


def register() -> None:
    """Log that the module is loadable, for post-install smoke testing.

    KiCad has no Python action-plugin registration API for the schematic
    editor, so this does not install a menu entry. It exists so that a user who
    runs ``import component_sync.plugin`` from the scripting console gets
    confirmation that imports and credentials resolve.
    """
    LOGGER.info(
        "component_sync loaded (digikey credentials %s). "
        "Note: KiCad 10 exposes no Python API for the Schematic/Symbol editor, "
        "so sync_selected() is best-effort. Use the CLI on the saved "
        ".kicad_sym, then Tools -> Refresh Symbol Libraries.",
        "present" if os.environ.get("DIGIKEY_CLIENT_ID") else "absent",
    )
