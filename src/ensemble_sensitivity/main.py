# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point for the ensemble-sensitivity package."""

import argparse
import logging

logger = logging.getLogger(__name__)


def parse_args() -> argparse.Namespace:
    """Parse command-line arguments.

    Returns:
        argparse.Namespace: Parsed command-line arguments.

    """
    parser = argparse.ArgumentParser(description="Ensemble Sensitivity Analysis")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    return parser.parse_args()


def main() -> None:
    """Entry point for the ensemble-sensitivity package."""
    args = parse_args()

    if args.verbose:
        logging.basicConfig(level=logging.DEBUG)
    else:
        logging.basicConfig(level=logging.INFO)

    logger.info("Hello from ensemble-sensitivity!")
    logger.debug("Debugging is enabled.")


if __name__ == "__main__":
    main()
