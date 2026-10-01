"""Enrich CSV BOMs and KiCad symbol libraries with distributor part data.

The package is organised around two strategy abstractions:

* :class:`~component_sync.providers.base.BaseProvider` -- where part data comes
  from (DigiKey today, Mouser or LCSC tomorrow).
* :class:`~component_sync.processors.base.BaseProcessor` -- which file format
  is being enriched (CSV today, ``.kicad_sym`` today, ``.kicad_sch`` later).

Both are selected through small factories, so neither the CLI nor the KiCad
plugin needs to change when a new implementation is added.

Example:
    >>> from component_sync import ProviderFactory, CSVProcessor
    >>> provider = ProviderFactory.create("digikey")  # doctest: +SKIP
    >>> result = CSVProcessor(provider, dry_run=True).process(path)  # doctest: +SKIP
"""

from __future__ import annotations

from .exceptions import (
    ComponentSyncError,
    ConfigurationError,
    FileFormatError,
    PartNotFoundError,
    ProviderAPIError,
)
from .models import ChangeAction, ComponentData, ProcessResult, PropertyChange
from .processors import BaseProcessor, CSVProcessor, KiCadSymProcessor
from .providers import BaseProvider, DigiKeyProvider, ProviderFactory

__version__ = "0.1.0"

__all__ = [
    "BaseProcessor",
    "BaseProvider",
    "CSVProcessor",
    "ChangeAction",
    "ComponentData",
    "ComponentSyncError",
    "ConfigurationError",
    "DigiKeyProvider",
    "FileFormatError",
    "KiCadSymProcessor",
    "PartNotFoundError",
    "ProcessResult",
    "PropertyChange",
    "ProviderAPIError",
    "ProviderFactory",
    "__version__",
]
