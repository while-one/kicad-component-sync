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
from pathlib import Path

from .exceptions import ComponentSyncError
from .processors.base import BaseProcessor
from .processors.csv_processor import CSVProcessor
from .processors.kicad_processor import KiCadSymProcessor
from .providers.base import BaseProvider
from .providers.factory import ProviderFactory

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
        choices=ProviderFactory.available(),
        help="Distributor to query (default: digikey).",
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
        "-v",
        "--verbose",
        action="store_true",
        help="Enable debug logging.",
    )
    return parser


def _build_provider(args: argparse.Namespace) -> BaseProvider:
    """Construct the provider selected on the command line.

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
        processor_class = select_processor(input_file)
        with provider:
            processor = processor_class(provider, dry_run=args.dry_run)
            result = processor.process(input_file)
    except ComponentSyncError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130

    if not args.dry_run:
        print(result.summary())

    return 0 if not result.missing_parts or args.dry_run else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
