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
from ensemble_sensitivity.run_index import prune_index
from ensemble_sensitivity.sensitivity.command import SensitivityRequest, run_sensitivity
from ensemble_sensitivity.visualization import render_quicklooks

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

DEFAULT_LEAD_TIME_HOURS = 24
DEFAULT_STEP_HOURS = 24
DEFAULT_OUTPUT_DIR = Path("output")
PACKAGE_LOGGER_NAME = "ensemble_sensitivity"


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
    _add_sensitivity_parser(subparsers, shared)
    _add_app_parser(subparsers, shared)
    namespace = parser.parse_args(args)
    if namespace.verbose and namespace.quiet:
        parser.error("argument -q/--quiet: not allowed with argument -v/--verbose")
    return namespace


def configure_logging(*, verbose: bool = False, quiet: bool = False) -> None:
    """Configure application logging.

    Args:
        verbose: Whether to enable DEBUG-level logging for this package only; third-party
            loggers (matplotlib, cartopy, urllib3, PIL...) stay at INFO.
        quiet: Whether to log only errors.

    """
    level = logging.DEBUG if verbose else logging.ERROR if quiet else logging.INFO
    root_level = max(level, logging.INFO)
    root_logger = logging.getLogger()
    root_logger.setLevel(root_level)
    logging.getLogger(PACKAGE_LOGGER_NAME).setLevel(level)
    if not root_logger.handlers:
        logging.basicConfig(level=root_level)


def main(args: Sequence[str] | None = None) -> None:
    """Run the application or a PEARP data pipeline stage.

    Log records are routed through tqdm so they print above the progress bar.
    """
    parsed_args = parse_args(args)
    configure_logging(verbose=parsed_args.verbose, quiet=parsed_args.quiet)
    with logging_redirect_tqdm():
        _dispatch(parsed_args)


def _prune_run_index(run_dir: Path) -> None:
    """Purge index entries of the run's output root whose directory vanished."""
    resolved = run_dir.resolve()
    root = resolved.parent.parent if resolved.name == RAW_FETCH_DIRNAME else resolved.parent
    prune_index(root)


def _add_sensitivity_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    shared: argparse.ArgumentParser,
) -> None:
    """Declare the ``sensitivity`` subcommand."""
    parser = subparsers.add_parser(
        "sensitivity",
        parents=[shared],
        help="signed Pearson map between a target-zone mean and an earlier Z500 field",
        description=(
            "Correlate, over the 35 members, the area-weighted Z500 mean of a circular zone at "
            "the target lead with Z500 at every grid point at an earlier lead."
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--run-id", help="run UUID from runs_index.json (default: most recent)")
    parser.add_argument("--target-lead", type=int, required=True, help="target lead in hours")
    parser.add_argument(
        "--predictor-lead",
        type=int,
        help=(
            "earlier lead in hours, strictly below the target lead; by default every "
            "available lead up to the target is a target in turn, with all earlier leads "
            "as predictors"
        ),
    )
    parser.add_argument("--lat", type=float, required=True, help="zone centre latitude")
    parser.add_argument("--lon", type=float, required=True, help="zone centre longitude")
    parser.add_argument("--radius-km", type=float, required=True, help="zone radius in km")


def _run_sensitivity(parsed_args: argparse.Namespace) -> None:
    """Run the ``sensitivity`` command.

    Raises:
        SystemExit: If the computation or rendering fails.

    """
    try:
        run_sensitivity(
            SensitivityRequest(
                output_root=parsed_args.output_dir,
                run_id=parsed_args.run_id,
                target_lead_hours=parsed_args.target_lead,
                predictor_lead_hours=parsed_args.predictor_lead,
                center_latitude=parsed_args.lat,
                center_longitude=parsed_args.lon,
                radius_km=parsed_args.radius_km,
                show_progress=not parsed_args.quiet,
            )
        )
    except (OSError, RetrievalError, ValueError) as error:
        logger.error(  # ruff: ignore[error-instead-of-exception]
            "Unable to compute the sensitivity map: %s",
            error,
            exc_info=parsed_args.verbose,
        )
        raise SystemExit(1) from None


def _add_app_parser(
    subparsers: argparse._SubParsersAction[argparse.ArgumentParser],
    shared: argparse.ArgumentParser,
) -> None:
    """Declare the ``app`` subcommand."""
    parser = subparsers.add_parser(
        "app",
        parents=[shared],
        help="local application: two linked maps (target mean/spread and Pearson correlation)",
        description=(
            "Serve a local Panel/Bokeh application on 127.0.0.1: click the target map to set "
            "the zone centre, the correlation map is recomputed on the fly."
        ),
    )
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    parser.add_argument("--run-id", help="run UUID from runs_index.json (default: most recent)")
    parser.add_argument("--port", type=int, default=5006, help="local port (default: %(default)s)")
    parser.add_argument("--no-browser", action="store_true", help="do not open the default browser")


def _run_app(parsed_args: argparse.Namespace) -> None:
    """Run the ``app`` command.

    Raises:
        SystemExit: If the run cannot be opened.

    """
    from ensemble_sensitivity.app.view import serve  # ruff: ignore[import-outside-top-level]

    try:
        serve(
            parsed_args.output_dir,
            parsed_args.run_id,
            port=parsed_args.port,
            show=not parsed_args.no_browser,
        )
    except (OSError, RetrievalError, ValueError) as error:
        logger.error(  # ruff: ignore[error-instead-of-exception]
            "Unable to start the application: %s",
            error,
            exc_info=parsed_args.verbose,
        )
        raise SystemExit(1) from None


def _run_fetch(parsed_args: argparse.Namespace) -> None:
    """Run the ``fetch`` command.

    Raises:
        SystemExit: If the retrieval fails.

    """
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


def _run_plot(parsed_args: argparse.Namespace) -> None:
    """Run the ``plot`` command.

    Raises:
        SystemExit: If the rendering fails.

    """
    try:
        _prune_run_index(parsed_args.run_dir)
        output_paths = render_quicklooks(
            parsed_args.run_dir,
            lead_hours=parsed_args.lead_hours,
            members=parsed_args.members,
            output_dir=parsed_args.output_dir,
            output_path=parsed_args.output,
            show_progress=not parsed_args.quiet,
        )
    except (OSError, RetrievalError, ValueError) as error:
        logger.error(  # ruff: ignore[error-instead-of-exception]
            "Unable to render PEARP quicklook: %s",
            error,
            exc_info=parsed_args.verbose,
        )
        raise SystemExit(1) from None
    logger.info("Saved %d quicklook map(s)", len(output_paths))


def _dispatch(parsed_args: argparse.Namespace) -> None:
    """Run the selected command."""
    handlers = {
        "fetch": _run_fetch,
        "plot": _run_plot,
        "sensitivity": _run_sensitivity,
        "app": _run_app,
    }
    handler = handlers.get(parsed_args.command)
    if handler is not None:
        handler(parsed_args)
        return
    logger.info("Hello from ensemble-sensitivity!")
    logger.debug("Debugging is enabled.")


if __name__ == "__main__":
    main()
