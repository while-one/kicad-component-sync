"""Abstract base class and shared helpers for file processors."""

from __future__ import annotations

import logging
import os
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path

from ..cache import LookupCache
from ..exceptions import (  # noqa: F401  (FileFormatError re-exported for subclasses)
    ComponentSyncError,
    FileFormatError,
    RateLimitError,
)
from ..models import ChangeAction, ComponentData, ProcessResult, PropertyChange
from ..providers.base import BaseProvider
from ..reporting import Palette, default_palette, render_report
from ..selection import FieldSelection

__all__ = ["BaseProcessor", "atomic_write"]

LOGGER = logging.getLogger(__name__)


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
        fields: Restricts which fields this run may change.
        colour: When false the report is plain text regardless of destination.
        width: Maximum width for each value rendered in the report.
    """

    def __init__(
        self,
        provider: BaseProvider,
        dry_run: bool = False,
        fields: FieldSelection | None = None,
        colour: bool | None = None,
        width: int = 60,
        sources: tuple[BaseProvider, ...] = (),
    ) -> None:
        """Initialise the processor.

        Args:
            provider: Data provider, consulted for component values.
            dry_run: When true, no file on disk is modified.
            fields: Restricts which fields may change; unrestricted by default.
            colour: Force colour on or off, or ``None`` to detect from the
                destination stream.
            width: Maximum width for each value rendered in the report.
            sources: Sourcing providers, consulted after the data provider and
                contributing purchasing links only.
        """
        self.provider = provider
        self.sources = sources
        self.dry_run = dry_run
        self.cache = LookupCache()
        self.fields = fields if fields is not None else FieldSelection()
        self._colour = colour
        self.width = width

    def palette(self) -> Palette:
        """Return the colour helper this processor should report with.

        Returns:
            A palette honouring the instance setting and the ``NO_COLOR``
            convention.
        """
        return default_palette(force=self._colour)

    def _report(self, result: ProcessResult) -> None:
        """Print the grouped review report.

        Args:
            result: The result being reported.
        """
        print(
            render_report(
                result,
                palette=self.palette(),
                selection=self.fields,
                width=self.width,
            ),
            end="",
        )

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
        MPN is data the author needs to see, not a crash.

        Args:
            mpn: Manufacturer part number to resolve.

        Returns:
            Component data, or ``None`` when the provider has no match.
        """
        return self.cache.fetch(self.provider, mpn)

    def _desired(
        self, mpn: str, existing: dict[str, str]
    ) -> tuple[dict[str, str] | None, str]:
        """Return every field this run would like to set for one part number.

        The data provider is consulted first, because it decides what the
        component *is*. Each sourcing provider then contributes its own links.

        A sourcing provider may only **add** a link: if the file already holds a
        value for that field, its link is dropped. In this library 46 of the 57
        existing ``Mouser`` values are ``mou.sr`` short links to a different
        reseller, and those are the author's curation, not an error to correct.
        The rule is scoped to contributed links precisely so it does not block
        the data provider from correcting a genuinely wrong value.

        Args:
            mpn: The manufacturer part number to resolve.
            existing: Fields currently present on the component.

        Returns:
            A ``(properties, reason)`` pair. ``properties`` is ``None`` when the
            data provider could not resolve the part; ``reason`` is non-empty
            only for a provider fault.
        """
        data, reason = self._try_resolve(mpn)
        if data is None:
            return None, reason

        properties = data.as_properties()
        for source in self.sources:
            manufacturer = existing.get("Manufacturer", "").strip()
            try:
                links = self.cache.fetch_links(source, mpn, manufacturer)
            except ComponentSyncError as exc:
                LOGGER.info("Sourcing lookup failed for %r at %s: %s", mpn, source.name, exc)
                continue
            for name, url in links.items():
                if existing.get(name, "").strip():
                    continue
                properties[name] = url
        return properties, ""

    def _try_resolve(self, mpn: str) -> tuple[ComponentData | None, str]:
        """Resolve one MPN, converting a provider fault into a reportable note.

        A failure part-way through a run used to abort the whole thing, so an
        exhausted rate limit on part 57 of 57 threw away the other 56 results and
        the author got no report at all. An ordinary fault is now recorded
        against that one part and the run continues, so the work already done is
        still reported. Faults are never cached, so a later run retries them.

        An exhausted *daily* quota is the exception and stops the run. Once the
        daily allowance is gone every remaining request will be refused too, so
        continuing would only print the same refusal 56 more times and bury the
        single fact the author needs. A per-minute burst does not stop the run,
        because the provider already retried it and other parts may succeed.

        Args:
            mpn: Manufacturer part number to resolve.

        Returns:
            A ``(data, reason)`` pair. ``data`` is ``None`` for both an unstocked
            part and a failed lookup; ``reason`` is non-empty only for the latter.

        Raises:
            RateLimitError: If the distributor's daily quota is exhausted.
        """
        try:
            return self._resolve(mpn), ""
        except RateLimitError as exc:
            if exc.daily:
                raise
            LOGGER.warning("Lookup failed for %r: %s", mpn, exc)
            return None, exc.message
        except ComponentSyncError as exc:
            LOGGER.warning("Lookup failed for %r: %s", mpn, exc)
            return None, exc.message

    def _classify(
        self,
        identifier: str,
        existing: dict[str, str],
        desired: dict[str, str],
    ) -> list[PropertyChange]:
        """Compare desired fields against current ones.

        The active :class:`~component_sync.selection.FieldSelection` is applied
        here, before any edit is planned, so a filtered field is never written
        even transiently.

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
            if not self.fields.admits(name):
                self.fields.note_suppressed(name)
                continue
            if self.fields.is_cosmetic(existing.get(name), value):
                self.fields.note_suppressed(name)
                continue
            if name not in existing:
                changes.append(PropertyChange(identifier, name, None, value, ChangeAction.ADD))
            elif existing[name] != value:
                changes.append(
                    PropertyChange(identifier, name, existing[name], value, ChangeAction.UPDATE)
                )
        return changes
