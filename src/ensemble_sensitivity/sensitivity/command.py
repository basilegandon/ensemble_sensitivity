# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""``sensitivity`` command: resolve a run in the index, compute, render."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ensemble_sensitivity.data_retrieval.wcs_retrieval import MEMBERS, RAW_FETCH_DIRNAME
from ensemble_sensitivity.field_io import (
    GRID_NI,
    GRID_NJ,
    manifest_initialization,
    read_manifest,
)
from ensemble_sensitivity.run_index import load_index, prune_index
from ensemble_sensitivity.sensitivity.compute import sensitivity_from_run
from ensemble_sensitivity.sensitivity.render import SensitivityFigure, render_sensitivity

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class SensitivityCommandError(ValueError):
    """The requested run cannot be resolved from the run index."""


@dataclass(frozen=True, slots=True)
class SensitivityRequest:
    """Parameters of one sensitivity map."""

    output_root: Path
    run_id: str | None
    target_lead_hours: int
    predictor_lead_hours: int
    center_latitude: float
    center_longitude: float
    radius_km: float


def resolve_run(output_root: Path, run_id: str | None) -> tuple[str, Path]:
    """Find a run in the index; the most recently created one when ``run_id`` is None.

    Returns:
        The run UUID and its directory.

    Raises:
        SensitivityCommandError: If the index is empty, the run is unknown, or its data
            directory is not a complete retrieval.

    """
    prune_index(output_root)
    runs = load_index(output_root)
    if not runs:
        raise SensitivityCommandError(f"No indexed run under {output_root}: run fetch first.")
    if run_id is None:
        run_id = max(runs, key=lambda key: str(runs[key]["created_at_utc"]))
    elif run_id not in runs:
        known = ", ".join(sorted(runs))
        raise SensitivityCommandError(f"Unknown run {run_id!r}; indexed runs: {known}")
    return run_id, output_root / str(runs[run_id]["directory"])


def output_stem(request: SensitivityRequest) -> str:
    """Build the output file stem from the request parameters.

    Returns:
        A name unique per target, predictor, centre and radius.

    """
    return (
        f"sensitivity_t{request.target_lead_hours:03}_from_t{request.predictor_lead_hours:03}"
        f"_lat{float(request.center_latitude):+}_lon{float(request.center_longitude):+}"
        f"_r{float(request.radius_km)}km"
    )


def run_sensitivity(request: SensitivityRequest) -> Path:
    """Compute and render one sensitivity map under ``<uuid>/maps``.

    Returns:
        Path of the PNG; its JSON provenance sits next to it.

    """
    run_id, run_dir = resolve_run(request.output_root, request.run_id)
    raw_dir = run_dir / RAW_FETCH_DIRNAME
    manifest = read_manifest(raw_dir)
    result = sensitivity_from_run(
        raw_dir,
        members=MEMBERS,
        target_lead_hours=request.target_lead_hours,
        predictor_lead_hours=request.predictor_lead_hours,
        center_latitude=request.center_latitude,
        center_longitude=request.center_longitude,
        radius_km=request.radius_km,
    )
    output_path = run_dir / "maps" / f"{output_stem(request)}.png"
    render_sensitivity(
        SensitivityFigure(
            result=result,
            longitudes=np.arange(GRID_NI, dtype=np.float64) * 0.25,
            latitudes=np.linspace(90.0, -90.0, GRID_NJ),
            run_id=run_id,
            coverage_id=manifest.get("coverage_id"),
            initialization=manifest_initialization(manifest),
            center_latitude=request.center_latitude,
            center_longitude=request.center_longitude,
            radius_km=request.radius_km,
        ),
        output_path,
    )
    logger.info("Saved sensitivity map %s", output_path)
    return output_path
