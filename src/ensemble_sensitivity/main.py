# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Entry point for the ensemble-sensitivity package."""

import argparse
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from tqdm.contrib.logging import logging_redirect_tqdm

from ensemble_sensitivity.data_retrieval.wcs_retrieval import (
    RAW_FETCH_DIRNAME,
    RetrievalError,
    RetrievalOptions,
    retrieve_latest_complete_run,
)
from ensemble_sensitivity.visualization import render_quicklooks

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

DEFAULT_LEAD_TIME_HOURS = 24
DEFAULT_STEP_HOURS = 24
DEFAULT_OUTPUT_DIR = Path("output")


def _verbosity_parser(*, default: bool | str) -> argparse.ArgumentParser:
    """Build the shared ``-v``/``-q`` options.

    Args:
        default: Default value for both flags; ``argparse.SUPPRESS`` keeps values parsed
            before a subcommand from being overwritten by the subparser.

    Returns:
        A parent parser with mutually exclusive verbose and quiet flags.

    """
    parser = argparse.ArgumentParser(add_help=False)
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        default=default,
        help="log every validated field",
    )
    group.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        default=default,
        help="no progress bar and no log output except errors",
    )
    return parser


def parse_args(args: Sequence[str] | None = None) -> argparse.Namespace:
    """Parse command-line arguments.

    Args:
        args: Command-line arguments, or None to use sys.argv.

    Returns:
        Parsed command-line arguments.

    """
    parser = argparse.ArgumentParser(
        description="Ensemble Sensitivity Analysis",
        parents=[_verbosity_parser(default=False)],
    )
    subparsers = parser.add_subparsers(dest="command")
    shared = _verbosity_parser(default=argparse.SUPPRESS)
    fetch_parser = subparsers.add_parser(
        "fetch",
        parents=[shared],
        help="retrieve the latest complete PEARP Z500 run (GRIB, one field per member and lead)",
        description=(
            "Retrieve Z500 for all 35 PEARP members at every lead from 0 h up to the lead time, "
            "every STEP hours counted back from the lead time."
        ),
    )
    fetch_parser.add_argument(
        "-l",
        "--lead-time",
        type=int,
        default=DEFAULT_LEAD_TIME_HOURS,
        help="lead time of interest in hours, a multiple of 3 up to 102 (default: %(default)s)",
    )
    fetch_parser.add_argument(
        "-s",
        "--step",
        type=int,
        default=DEFAULT_STEP_HOURS,
        help="interval between retrieved leads in hours, a multiple of 3 (default: %(default)s)",
    )
    fetch_parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    fetch_parser.add_argument("--env-file", type=Path, default=Path(".env"))
    fetch_parser.add_argument(
        "--coverage-id",
        help="retrieve only this exact advertised WCS coverage instead of searching latest first",
    )
    plot_parser = subparsers.add_parser(
        "plot",
        parents=[shared],
        help="render one field from a complete retrieval manifest",
    )
    plot_parser.add_argument(
        "--run-dir",
        type=Path,
        required=True,
        help="run directory printed by fetch (or its raw_fetch subdirectory)",
    )
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


def configure_logging(*, verbose: bool = False, quiet: bool = False) -> None:
    """Configure application logging.

    Args:
        verbose: Whether to enable DEBUG-level logging.
        quiet: Whether to log only warnings and errors.

    """
    level = logging.DEBUG if verbose else logging.WARNING if quiet else logging.INFO
    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    if not root_logger.handlers:
        logging.basicConfig(level=level)


def main(args: Sequence[str] | None = None) -> None:
    """Run the application or a PEARP data pipeline stage.

    Log records are routed through tqdm so they print above the progress bar.
    """
    parsed_args = parse_args(args)
    configure_logging(verbose=parsed_args.verbose, quiet=parsed_args.quiet)
    with logging_redirect_tqdm():
        _dispatch(parsed_args)


def _dispatch(parsed_args: argparse.Namespace) -> None:
    """Run the selected command.

    Raises:
        SystemExit: If a requested retrieval or visualization stage fails.

    """
    if parsed_args.command == "fetch":
        try:
            run_dir = retrieve_latest_complete_run(
                RetrievalOptions(
                    target_lead_hours=parsed_args.lead_time,
                    step_hours=parsed_args.step,
                    output_dir=parsed_args.output_dir,
                    env_file=parsed_args.env_file,
                    coverage_id=parsed_args.coverage_id,
                    show_progress=not parsed_args.quiet,
                )
            )
        except (OSError, RetrievalError, ValueError) as error:
            logger.error(  # ruff: ignore[error-instead-of-exception]
                "Unable to retrieve a complete PEARP run: %s",
                error,
                exc_info=parsed_args.verbose,
            )
            raise SystemExit(1) from None
        logger.info("Raw GRIB fields saved to %s", (run_dir / RAW_FETCH_DIRNAME).resolve())
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
        except (OSError, RetrievalError, ValueError) as error:
            logger.error(  # ruff: ignore[error-instead-of-exception]
                "Unable to render PEARP quicklook: %s",
                error,
                exc_info=parsed_args.verbose,
            )
            raise SystemExit(1) from None
        logger.info("Saved %d quicklook map(s)", len(output_paths))
        return

    logger.info("Hello from ensemble-sensitivity!")
    logger.debug("Debugging is enabled.")


if __name__ == "__main__":
    main()
