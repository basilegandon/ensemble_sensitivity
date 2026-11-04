# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Orchestrate the sensitivity map: target per member, then Pearson map at an earlier lead."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ensemble_sensitivity.field_io import GRID_NI, GRID_NJ, load_field, read_manifest
from ensemble_sensitivity.sensitivity.statistics import (
    MIN_MEMBERS,
    StatisticsError,
    pearson_map,
    weighted_mean,
)
from ensemble_sensitivity.sensitivity.zone import circular_zone_weights

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence
    from pathlib import Path

    from numpy.typing import NDArray

    from ensemble_sensitivity.sensitivity.statistics import PearsonMap

LOGGER = logging.getLogger(__name__)

# (member, lead_hours) -> field of shape (lat, lon) in native grid order.
FieldLoader = Callable[[int, int], "NDArray[np.floating]"]


@dataclass(frozen=True, slots=True)
class SensitivityResult:
    """Sensitivity of a zone-mean target (target lead) to Z500 at the predictor lead."""

    correlation: NDArray[np.float64]
    target_values: NDArray[np.float64]
    constant_points: int
    zone_cell_count: int
    member_count: int
    target_lead_hours: int
    predictor_lead_hours: int


def compute_sensitivity(
    load: FieldLoader,
    latitudes: NDArray[np.float64],
    longitudes: NDArray[np.float64],
    *,
    members: Sequence[int],
    target_lead_hours: int,
    predictor_lead_hours: int,
    center_latitude: float,
    center_longitude: float,
    radius_km: float,
) -> SensitivityResult:
    """Compute the signed Pearson sensitivity map; one field is held in memory at a time.

    Returns:
        Correlation map (NaN where undefined), per-member targets and diagnostics.

    """
    _validate_request(members, target_lead_hours, predictor_lead_hours)
    weights = circular_zone_weights(
        latitudes,
        longitudes,
        center_latitude=center_latitude,
        center_longitude=center_longitude,
        radius_km=radius_km,
    )
    target = np.array(
        [weighted_mean(load(member, target_lead_hours), weights) for member in members],
        dtype=np.float64,
    )
    result: PearsonMap = pearson_map(_member_fields(load, members, predictor_lead_hours), target)
    if result.constant_points:
        LOGGER.warning(
            "%d grid points have identical values across members: correlation undefined (NaN).",
            result.constant_points,
        )
    return SensitivityResult(
        correlation=result.correlation,
        target_values=target,
        constant_points=result.constant_points,
        zone_cell_count=int(np.count_nonzero(weights)),
        member_count=len(members),
        target_lead_hours=target_lead_hours,
        predictor_lead_hours=predictor_lead_hours,
    )


def sensitivity_from_run(
    run_dir: Path,
    *,
    members: Sequence[int],
    target_lead_hours: int,
    predictor_lead_hours: int,
    center_latitude: float,
    center_longitude: float,
    radius_km: float,
) -> SensitivityResult:
    """Compute the sensitivity map from a complete retrieval directory.

    Returns:
        The sensitivity result for the run.

    """
    manifest = read_manifest(run_dir)

    def load(member: int, lead_hours: int) -> NDArray[np.floating]:
        return load_field(run_dir, manifest, member=member, lead_hours=lead_hours).values

    return compute_sensitivity(
        load,
        np.linspace(90.0, -90.0, GRID_NJ),
        np.arange(GRID_NI, dtype=np.float64) * 0.25,
        members=members,
        target_lead_hours=target_lead_hours,
        predictor_lead_hours=predictor_lead_hours,
        center_latitude=center_latitude,
        center_longitude=center_longitude,
        radius_km=radius_km,
    )


def _member_fields(
    load: FieldLoader,
    members: Sequence[int],
    lead_hours: int,
) -> Iterator[NDArray[np.floating]]:
    for member in members:
        yield load(member, lead_hours)


def _validate_request(members: Sequence[int], target_lead: int, predictor_lead: int) -> None:
    if len(set(members)) != len(members):
        raise StatisticsError("Duplicate members in the request.")
    if len(members) < MIN_MEMBERS:
        raise StatisticsError(f"At least {MIN_MEMBERS} members are required: {len(members)}.")
    if not 0 <= predictor_lead < target_lead:
        raise StatisticsError(
            f"Predictor lead ({predictor_lead} h) must be non-negative and earlier than the "
            f"target lead ({target_lead} h).",
        )
