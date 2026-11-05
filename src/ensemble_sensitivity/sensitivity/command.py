# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""``sensitivity`` command: resolve a run in the index, compute, render."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from tqdm import tqdm

from ensemble_sensitivity.data_retrieval.wcs_retrieval import MEMBERS, RAW_FETCH_DIRNAME
from ensemble_sensitivity.field_io import (
    grid_axes,
    manifest_initialization,
    manifest_leads,
    read_manifest,
)
from ensemble_sensitivity.run_index import load_index, prune_index
from ensemble_sensitivity.sensitivity.compute import (
    correlate_with_target,
    run_field_loader,
    zone_target_values,
)
from ensemble_sensitivity.sensitivity.render import SensitivityFigure, render_sensitivity
from ensemble_sensitivity.sensitivity.zone import circular_zone_weights

if TYPE_CHECKING:
    from pathlib import Path

    from ensemble_sensitivity.sensitivity.compute import SensitivityResult

logger = logging.getLogger(__name__)


class SensitivityCommandError(ValueError):
    """The requested run cannot be resolved from the run index."""


@dataclass(frozen=True, slots=True)
class SensitivityRequest:
    """Parameters of one sensitivity map."""

    output_root: Path
    run_id: str | None
    target_lead_hours: int
    predictor_lead_hours: int | None
    center_latitude: float
    center_longitude: float
    radius_km: float
    show_progress: bool = True


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


def output_stem(request: SensitivityRequest, target_lead: int, predictor_lead: int) -> str:
    """Build the output file stem for one (target, predictor) pair of the request.

    Returns:
        A name unique per target, predictor, centre and radius.

    """
    return (
        f"sensitivity_t{target_lead:03}_from_t{predictor_lead:03}"
        f"_lat{float(request.center_latitude):+}_lon{float(request.center_longitude):+}"
        f"_r{float(request.radius_km)}km"
    )


def plan_pairs(
    leads: tuple[int, ...],
    target_lead_hours: int,
    predictor_lead_hours: int | None,
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    """List the (target, predictors) pairs to compute, newest target first.

    Without a predictor, every available lead up to the target is a target in turn, each
    paired with all strictly earlier available leads (the predictor equal to the target is
    excluded: its correlation with the target is trivial).

    Returns:
        ``(target_lead, predictor_leads)`` tuples; a single pair when a predictor is given.

    """
    _require_lead("Target", target_lead_hours, leads)
    if predictor_lead_hours is not None:
        _require_lead("Predictor", predictor_lead_hours, leads, below=target_lead_hours)
        return ((target_lead_hours, (predictor_lead_hours,)),)
    return _full_plan(leads, target_lead_hours)


def _full_plan(
    leads: tuple[int, ...],
    target_lead_hours: int,
) -> tuple[tuple[int, tuple[int, ...]], ...]:
    """Pair each lead up to the target (newest first) with all strictly earlier leads.

    Returns:
        ``(target_lead, predictor_leads)`` tuples.

    Raises:
        SensitivityCommandError: If the target is the earliest lead.

    """
    targets = [lead for lead in leads[1:] if lead <= target_lead_hours]
    if not targets:
        message = f"Target lead {target_lead_hours} h has no earlier available lead: {leads}"
        raise SensitivityCommandError(message)
    return tuple((target, leads[: leads.index(target)]) for target in reversed(targets))


def _require_lead(
    label: str, lead: int, leads: tuple[int, ...], *, below: int | None = None
) -> None:
    """Check that ``lead`` is an available lead, strictly below ``below`` when given.

    Raises:
        SensitivityCommandError: If the lead is not usable.

    """
    if lead not in leads or (below is not None and lead >= below):
        bound = "" if below is None else f" earlier than {below} h"
        message = f"{label} lead {lead} h must be an available lead{bound}; available: {leads}"
        raise SensitivityCommandError(message)


def _figure(
    request: SensitivityRequest,
    result: SensitivityResult,
    run_id: str,
    manifest: dict[str, object],
) -> SensitivityFigure:
    latitudes, longitudes = grid_axes()
    return SensitivityFigure(
        result=result,
        longitudes=longitudes,
        latitudes=latitudes,
        run_id=run_id,
        coverage_id=manifest.get("coverage_id"),
        initialization=manifest_initialization(manifest),
        center_latitude=request.center_latitude,
        center_longitude=request.center_longitude,
        radius_km=request.radius_km,
    )


def run_sensitivity(request: SensitivityRequest) -> tuple[Path, ...]:
    """Compute and render the requested sensitivity maps under ``<uuid>/maps``.

    Returns:
        Paths of the PNGs, target-major (latest target first); each JSON provenance sits
        next to its PNG.

    """
    run_id, run_dir = resolve_run(request.output_root, request.run_id)
    raw_dir = run_dir / RAW_FETCH_DIRNAME
    manifest = read_manifest(raw_dir)
    plan = plan_pairs(
        manifest_leads(manifest), request.target_lead_hours, request.predictor_lead_hours
    )
    latitudes, longitudes = grid_axes()
    weights = circular_zone_weights(
        latitudes,
        longitudes,
        center_latitude=request.center_latitude,
        center_longitude=request.center_longitude,
        radius_km=request.radius_km,
    )
    load = run_field_loader(raw_dir, manifest)
    outputs: list[Path] = []
    with tqdm(
        total=sum(len(predictors) for _, predictors in plan),
        desc="Sensitivity",
        unit="map",
        dynamic_ncols=True,
        disable=not request.show_progress,
    ) as progress:
        for target_lead, predictors in plan:
            target = zone_target_values(load, weights, MEMBERS, target_lead)
            for predictor_lead in predictors:
                progress.set_description(f"Sensitivity | t+{target_lead}h from t+{predictor_lead}h")
                result = correlate_with_target(
                    load,
                    weights,
                    MEMBERS,
                    target,
                    target_lead_hours=target_lead,
                    predictor_lead_hours=predictor_lead,
                )
                name = output_stem(request, target_lead, predictor_lead)
                path = run_dir / "maps" / f"{name}.png"
                render_sensitivity(_figure(request, result, run_id, manifest), path)
                outputs.append(path)
                progress.update()
    logger.info("Saved %d sensitivity maps under %s", len(outputs), run_dir / "maps")
    return tuple(outputs)
