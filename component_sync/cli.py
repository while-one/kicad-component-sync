"""Command line entry point for :mod:`component_sync`.

Example:
    $ python -m component_sync.cli bom.csv --provider digikey --dry-run
    $ python -m component_sync.cli lib.kicad_sym -p digikey
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Sequence
from contextlib import ExitStack
from pathlib import Path

from .exceptions import ComponentSyncError, RateLimitError
from .processors.base import BaseProcessor
from .processors.csv_processor import CSVProcessor
from .processors.kicad_processor import KiCadSymProcessor
from .providers.base import BaseProvider, ProviderRole
from .providers.factory import ProviderFactory
from .selection import ComponentFilter, FieldSelection

__all__ = ["main", "build_parser", "select_processor"]

LOGGER = logging.getLogger("component_sync")

#: File suffix to processor mapping.
_PROCESSORS: dict[str, type[BaseProcessor]] = {
    ".csv": CSVProcessor,
    ".kicad_sym": KiCadSymProcessor,
}


def select_processor(path: Path) -> type[BaseProcessor]:
    """Return the processor class that handles ``path``.

    Args:
        path: Path to the input file.

    Returns:
        The matching processor class.

    Raises:
        ComponentSyncError: If the extension has no registered processor.
    """
    suffix = path.suffix.lower()
    processor = _PROCESSORS.get(suffix)
    if processor is None:
        supported = ", ".join(sorted(_PROCESSORS))
        raise ComponentSyncError(
            f"Unsupported input type {suffix!r} for {path}. Supported: {supported}"
        )
    return processor


def build_parser() -> argparse.ArgumentParser:
    """Build the argument parser for the command line interface.

    Returns:
        The configured parser.
    """
    parser = argparse.ArgumentParser(
        prog="component-sync",
        description=(
            "Enrich a CSV BOM or KiCad symbol library with parametric data "
            "from a component distributor."
        ),
    )
    parser.add_argument(
        "input_file",
        type=Path,
        help="Path to a .csv BOM or .kicad_sym symbol library.",
    )
    parser.add_argument(
        "-p",
        "--provider",
        default="digikey",
        choices=ProviderFactory.available(ProviderRole.DATA),
        help=(
            "Data provider to query for component values (default: digikey). "
            "Sourcing providers are not offered here; they run in sequence."
        ),
    )
    parser.add_argument(
        "--component",
        default=None,
        metavar="TEXT",
        help=(
            "Restrict the run to components whose symbol name or part number "
            "contains TEXT. Comma separated for several. Matched case "
            "insensitively, and applied before any lookup, so an excluded "
            "component costs no API request."
        ),
    )
    parser.add_argument(
        "--no-source",
        action="store_true",
        help=(
            "Skip the sourcing pass. By default every configured sourcing "
            "provider is consulted after the data provider to add purchasing "
            "links."
        ),
    )
    parser.add_argument(
        "-n",
        "--dry-run",
        action="store_true",
        help="Validate and report proposed changes without writing anything.",
    )
    parser.add_argument(
        "--client-id",
        default=None,
        help="Override the DIGIKEY_CLIENT_ID environment variable.",
    )
    parser.add_argument(
        "--client-secret",
        default=None,
        help="Override the DIGIKEY_CLIENT_SECRET environment variable.",
    )
    parser.add_argument(
        "--only",
        default=None,
        metavar="FIELDS",
        help=(
            "Comma separated fields the run may change, e.g. --only Value,Package. "
            "Everything else is left alone."
        ),
    )
    parser.add_argument(
        "--skip",
        default=None,
        metavar="FIELDS",
        help=(
            "Comma separated fields to leave alone, e.g. --skip Description. "
            "Ignored when --only is given."
        ),
    )
    parser.add_argument(
        "--ignore-case",
        action="store_true",
        help=(
            "Treat a change that differs only in letter case as no change, so "
            "'Yageo' -> 'YAGEO' is not proposed."
        ),
    )
    parser.add_argument(
        "--no-color",
        action="store_true",
        help="Disable colour even when writing to a terminal.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=60,
        metavar="N",
        help="Maximum width of each value in the report (default: 60).",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def _build_provider(args: argparse.Namespace) -> BaseProvider:
    """Construct the data provider selected on the command line.

    Args:
        args: Parsed command line arguments.

    Returns:
        The configured provider instance.
    """
    overrides: dict[str, object] = {}
    if args.client_id:
        overrides["client_id"] = args.client_id
    if args.client_secret:
        overrides["client_secret"] = args.client_secret
    return ProviderFactory.create(args.provider, **overrides)


def _build_sources(args: argparse.Namespace) -> tuple[BaseProvider, ...]:
    """Construct the sourcing providers to run after the data provider.

    Every registered sourcing provider is used. One that has no credentials is
    skipped with a note rather than being treated as an error, so a run does not
    fail because an optional distributor was never configured.

    Args:
        args: Parsed command line arguments.

    Returns:
        The configured sourcing providers, in registration order.
    """
    if args.no_source:
        return ()
    sources: list[BaseProvider] = []
    for name in ProviderFactory.available(ProviderRole.SOURCE):
        provider = ProviderFactory.create(name)
        try:
            provider.authenticate()
        except ComponentSyncError as exc:
            LOGGER.info("Skipping sourcing provider %s: %s", name, exc.message)
            provider.close()
            continue
        sources.append(provider)
    return tuple(sources)


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line interface.

    Args:
        argv: Argument vector, defaulting to :data:`sys.argv`.

    Returns:
        Process exit status: ``0`` on success, ``1`` on an expected failure.
    """
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    input_file: Path = args.input_file
    if not input_file.is_file():
        print(f"error: input file not found: {input_file}", file=sys.stderr)
        return 1

    try:
        provider = _build_provider(args)
        sources = _build_sources(args)
        processor_class = select_processor(input_file)
        selection = FieldSelection.build(
            only=args.only,
            skip=args.skip,
            ignore_case=args.ignore_case,
        )
        with ExitStack() as stack:
            stack.enter_context(provider)
            for source in sources:
                stack.enter_context(source)
            processor = processor_class(
                provider,
                dry_run=args.dry_run,
                fields=selection,
                colour=False if args.no_color else None,
                width=args.width,
                sources=sources,
                components=ComponentFilter.build(args.component),
            )
            result = processor.process(input_file)
    except ComponentSyncError as exc:
        if isinstance(exc, RateLimitError):
            print(f"error: {exc.message}", file=sys.stderr)
            print(
                "\nNothing was written. The quota is per calendar day and resets "
                "on its own; re-run afterwards.",
                file=sys.stderr,
            )
            return 2
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if not args.dry_run:
        print(result.summary())

    return 0 if not result.incomplete or args.dry_run else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
