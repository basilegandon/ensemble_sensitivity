# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point for the ensemble-sensitivity package."""

import argparse
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        args: Command-line arguments, or None to use sys.argv.

    Returns:
        Parsed command-line arguments.

    """
    parser = argparse.ArgumentParser(description="Ensemble Sensitivity Analysis")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable verbose output",
    )
    return parser.parse_args(args)


def configure_logging(*, verbose: bool = False) -> None:
    """Configure application logging.

    Args:
        verbose: Whether to enable DEBUG-level logging.

    """
    level = logging.DEBUG if verbose else logging.INFO
    logging.basicConfig(level=level)


def main(args: Sequence[str] | None = None) -> None:
    """Run the application."""
    parsed_args = parse_args(args)
    configure_logging(verbose=parsed_args.verbose)

    logger.info("Hello from ensemble-sensitivity!")
    logger.debug("Debugging is enabled.")


if __name__ == "__main__":
    main()
