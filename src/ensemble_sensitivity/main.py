# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point for the ensemble-sensitivity package."""

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ensemble_sensitivity.visualization import render_quicklooks
from ensemble_sensitivity.wcs_retrieval import (
    RetrievalError,
    RetrievalOptions,
    retrieve_latest_complete_run,
)

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
    subparsers = parser.add_subparsers(dest="command")
    fetch_parser = subparsers.add_parser(
        "fetch",
        help="retrieve the latest complete PEARP Z500 run",
    )
    fetch_parser.add_argument("--target-lead-hours", type=int, default=24)
    fetch_parser.add_argument("--step-hours", type=int, default=24)
    fetch_parser.add_argument("--output-dir", type=Path, default=Path("data") / "pearp")
    fetch_parser.add_argument("--env-file", type=Path, default=Path(".env"))
    fetch_parser.add_argument(
        "--coverage-id",
        help="retrieve only this exact advertised WCS coverage instead of searching latest first",
    )
    plot_parser = subparsers.add_parser(
        "plot",
        help="render one field from a complete retrieval manifest",
    )
    plot_parser.add_argument("--run-dir", type=Path, required=True)
    plot_parser.add_argument(
        "--lead-hours",
        required=True,
        help="comma-separated forecast leads in hours, or '*' for all retrieved leads",
    )
    plot_parser.add_argument(
        "--members",
        "--member",
        default="0",
        help="comma-separated member IDs, or '*' for all 35 members",
    )
    plot_parser.add_argument(
        "--output-dir",
        type=Path,
        help="directory for batch maps (default: <run-dir>\\maps)",
    )
    plot_parser.add_argument("--output", type=Path)
    return parser.parse_args(args)


def configure_logging(*, verbose: bool = False) -> None:
    """Configure application logging.

    Args:
        verbose: Whether to enable DEBUG-level logging.

    """
    level = logging.DEBUG if verbose else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        logging.basicConfig(level=level)


def main(args: Sequence[str] | None = None) -> None:
    """Run the application or a PEARP data pipeline stage.

    Raises:
        SystemExit: If a requested retrieval or visualization stage fails.

    """
    parsed_args = parse_args(args)
    configure_logging(verbose=parsed_args.verbose)

    if parsed_args.command == "fetch":
        try:
            run_dir = retrieve_latest_complete_run(
                RetrievalOptions(
                    target_lead_hours=parsed_args.target_lead_hours,
                    step_hours=parsed_args.step_hours,
                    output_dir=parsed_args.output_dir,
                    env_file=parsed_args.env_file,
                    coverage_id=parsed_args.coverage_id,
                )
            )
        except OSError, RetrievalError, ValueError:
            logger.exception("Unable to retrieve a complete PEARP run")
            raise SystemExit(1) from None
        logger.info("Complete PEARP run saved to %s", run_dir.resolve())
        return

    if parsed_args.command == "plot":
        try:
            output_paths = render_quicklooks(
                parsed_args.run_dir,
                lead_hours=parsed_args.lead_hours,
                members=parsed_args.members,
                output_dir=parsed_args.output_dir,
                output_path=parsed_args.output,
            )
        except OSError, RetrievalError, ValueError:
            logger.exception("Unable to render PEARP quicklook")
            raise SystemExit(1) from None
        logger.info("Saved %d quicklook map(s)", len(output_paths))
        return

    logger.info("Hello from ensemble-sensitivity!")
    logger.debug("Debugging is enabled.")


if __name__ == "__main__":
    main()
