"""Abstract base class and shared helpers for file processors."""

from __future__ import annotations

import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from ..cache import LookupCache
from ..exceptions import FileFormatError  # noqa: F401  (re-exported for subclasses)
from ..models import ChangeAction, ComponentData, ProcessResult, PropertyChange
from ..providers.base import BaseProvider

__all__ = ["BaseProcessor", "atomic_write"]


def atomic_write(path: Path, text: str, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically.

    The payload is written to a temporary file in the same directory, flushed
    to disk, and only then moved over the destination with
    :func:`os.replace`. Because ``os.replace`` is atomic within a filesystem,
    an interrupted or failing run can never leave a half-written BOM or symbol
    library behind.

    Args:
        path: Destination file to create or overwrite.
        text: Full file contents to write.
        encoding: Text encoding to use.

    Raises:
        OSError: If the temporary file cannot be written or replaced.
    """
    directory = path.parent
    handle = tempfile.NamedTemporaryFile(
        mode="w",
        encoding=encoding,
        newline="",
        dir=directory,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    temporary = Path(handle.name)
    try:
        with handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


class BaseProcessor(ABC):
    """Contract shared by every input file format.

    A processor locates components in a file, resolves each manufacturer part
    number through the active provider, and writes normalised fields back.
    Subclasses decide how to locate components and how to mutate the file.

    Every instance owns one :class:`~component_sync.cache.LookupCache`, shared by
    all of its calls to :meth:`process`. A bill of materials names the same part
    many times over, so without the cache a run would issue one request per
    component instance rather than one per distinct part.

    Attributes:
        provider: Provider used to resolve part numbers.
        dry_run: When true, nothing is ever written to disk.
        cache: Memoises provider lookups for the lifetime of this processor.
    """

    def __init__(self, provider: BaseProvider, dry_run: bool = False) -> None:
        """Initialise the processor.

        Args:
            provider: Provider used to resolve part numbers.
            dry_run: When true, no file on disk is modified.
        """
        self.provider = provider
        self.dry_run = dry_run
        self.cache = LookupCache()

    @abstractmethod
    def process(self, file_path: Path, dry_run: bool = False) -> ProcessResult:
        """Enrich every component in ``file_path`` and return a report.

        Args:
            file_path: The file to process.
            dry_run: Overrides the instance setting for this call.

        Returns:
            A report of proposed or applied changes.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Shared helpers for subclasses
    # ------------------------------------------------------------------
    def _resolve(self, mpn: str) -> ComponentData | None:
        """Resolve one MPN through the cache, returning ``None`` when missing.

        A part number that is not stocked is reported to the caller, which lists
        it so the author can check it, rather than being raised: an unresolvable
        MPN is data the author needs to see, not a crash. Every other provider
        failure propagates, because that is a genuine fault worth stopping for.

        Args:
            mpn: Manufacturer part number to resolve.

        Returns:
            Component data, or ``None`` when the provider has no match.
        """
        return self.cache.fetch(self.provider, mpn)

    @staticmethod
    def _classify(
        identifier: str,
        existing: dict[str, str],
        desired: dict[str, str],
    ) -> list[PropertyChange]:
        """Compare desired fields against current ones.

        Args:
            identifier: MPN for CSV rows, or symbol name for KiCad symbols.
            existing: Currently present fields.
            desired: Fields the provider wants to set.

        Returns:
            One :class:`PropertyChange` per field that would change, in a
            stable order.
        """
        changes: list[PropertyChange] = []
        for name, value in desired.items():
            if name not in existing:
                changes.append(PropertyChange(identifier, name, None, value, ChangeAction.ADD))
            elif existing[name] != value:
                changes.append(
                    PropertyChange(identifier, name, existing[name], value, ChangeAction.UPDATE)
                )
        return changes
