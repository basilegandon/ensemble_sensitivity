# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Application model: run, selection, validation and the data behind the two maps (no UI)."""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

import contourpy
import numpy as np

from ensemble_sensitivity.app.engine import DEFAULT_CACHE_LEADS, SensitivityEngine
from ensemble_sensitivity.data_retrieval.wcs_retrieval import MEMBERS, RAW_FETCH_DIRNAME
from ensemble_sensitivity.field_io import (
    grid_axes,
    manifest_initialization,
    manifest_leads,
    read_manifest,
    recenter_longitudes,
)
from ensemble_sensitivity.run_index import load_index, prune_index
from ensemble_sensitivity.sensitivity.command import resolve_run
from ensemble_sensitivity.sensitivity.compute import run_field_loader
from ensemble_sensitivity.sensitivity.render import zone_outline

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

    from numpy.typing import NDArray

RADIUS_MIN_KM = 50.0
RADIUS_MAX_KM = 3000.0
DEFAULT_CENTER = (46.5, 2.5)
DEFAULT_RADIUS_KM = 300.0
DISPLAY_STRIDE = 2
ANTIMERIDIAN_JUMP_DEGREES = 180.0
LEVEL_STEPS = (1.0, 2.0, 5.0)
MIN_LEVEL_COUNT = 8


class AppSelectionError(ValueError):
    """The requested selection is not available in the run or is out of bounds."""


@dataclass(frozen=True, slots=True)
class Selection:
    """What the two maps show: target and predictor leads, zone centre and radius."""

    target_lead_hours: int
    predictor_lead_hours: int
    center_latitude: float
    center_longitude: float
    radius_km: float


@dataclass(frozen=True, slots=True)
class MapsData:
    """Arrays for the two linked maps, oriented south to north and from -180 degrees east.

    ``std`` and ``mean`` are the ensemble spread and mean at the target lead (right map);
    ``correlation`` is the signed Pearson map at the predictor lead (left map).
    """

    correlation: NDArray[np.float32]
    mean: NDArray[np.float32]
    std: NDArray[np.float32]
    levels: tuple[float, ...]
    outline_longitudes: NDArray[np.float64]
    outline_latitudes: NDArray[np.float64]
    zone_cell_count: int
    constant_points: int
    selection: Selection


def _validate_leads(selection: Selection, leads: tuple[int, ...]) -> None:
    """Check a selection against the available leads and the documented bounds.

    Raises:
        AppSelectionError: If a lead is unavailable or out of order, or the zone is invalid.

    """
    if selection.target_lead_hours not in leads[1:]:
        message = (
            f"Target lead {selection.target_lead_hours} h unavailable; choose among {leads[1:]}"
        )
        raise AppSelectionError(message)
    earlier = [lead for lead in leads if lead < selection.target_lead_hours]
    if selection.predictor_lead_hours not in earlier:
        message = (
            f"Predictor lead {selection.predictor_lead_hours} h must be an available lead "
            f"earlier than the target {selection.target_lead_hours} h: {earlier}"
        )
        raise AppSelectionError(message)


def _require_between(name: str, value: float, low: float, high: float, unit: str) -> None:
    if not (math.isfinite(value) and low <= value <= high):
        raise AppSelectionError(f"{name} must be in [{low:g}, {high:g}]{unit}: {value}")


def _validate_zone(selection: Selection) -> None:
    _require_between("Latitude", selection.center_latitude, -90.0, 90.0, "")
    if not math.isfinite(selection.center_longitude):
        raise AppSelectionError(f"Longitude must be finite: {selection.center_longitude}")
    _require_between("Radius", selection.radius_km, RADIUS_MIN_KM, RADIUS_MAX_KM, " km")


def validate_selection(selection: Selection, leads: tuple[int, ...]) -> None:
    """Check a selection against the available leads and the documented bounds."""
    _validate_leads(selection, leads)
    _validate_zone(selection)


def default_selection(leads: tuple[int, ...]) -> Selection:
    """Choose the latest lead as target and the lead before it as predictor.

    Returns:
        A valid default selection.

    Raises:
        AppSelectionError: If the run has fewer than two leads.

    """
    if len(leads) < 2:
        raise AppSelectionError(f"The run needs at least two leads: {leads}")
    return Selection(leads[-1], leads[-2], *DEFAULT_CENTER, DEFAULT_RADIUS_KM)


def wrap_longitude(longitude: float) -> float:
    """Wrap a longitude to [-180, 180).

    Returns:
        The equivalent longitude in degrees east.

    """
    return (longitude + 180.0) % 360.0 - 180.0


def contour_levels(field: NDArray[np.float32]) -> tuple[float, ...]:
    """Pick round contour levels giving at least eight lines over the field range.

    Returns:
        Increasing levels, multiples of 1, 2 or 5 times a power of ten.

    """
    low, high = float(np.min(field)), float(np.max(field))
    span = high - low
    if span <= 0.0:
        return ()
    exponent = math.floor(math.log10(span / MIN_LEVEL_COUNT))
    step = next(
        base * 10.0**exponent
        for base in (*reversed(LEVEL_STEPS), 10.0)
        if span / (base * 10.0**exponent) >= MIN_LEVEL_COUNT
    )
    first = math.ceil(low / step)
    return tuple(float(k * step) for k in range(first, math.floor(high / step) + 1))


def outline_with_breaks(
    center_latitude: float,
    center_longitude: float,
    radius_km: float,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Zone boundary with longitudes wrapped to [-180, 180) and NaN where it crosses ±180.

    Returns:
        Longitudes and latitudes, ready for a line glyph that breaks at NaN.

    """
    longitudes, latitudes = zone_outline(center_latitude, center_longitude, radius_km)
    wrapped = (longitudes + 180.0) % 360.0 - 180.0
    jumps = np.flatnonzero(np.abs(np.diff(wrapped)) > ANTIMERIDIAN_JUMP_DEGREES) + 1
    return np.insert(wrapped, jumps, np.nan), np.insert(latitudes, jumps, np.nan)


def to_display(field: NDArray[np.float32]) -> NDArray[np.float32]:
    """Reorder a native field for a bottom-left-origin image centred on 0 degrees.

    Returns:
        A contiguous, subsampled copy (rows south to north, columns from -180 degrees).

    """
    centred, _ = recenter_longitudes(field, np.zeros(field.shape[1]))
    return np.ascontiguousarray(centred[::-DISPLAY_STRIDE, ::DISPLAY_STRIDE])


def display_axes(
    latitudes: NDArray[np.float64],
    longitudes: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Longitude and latitude axes matching ``to_display`` (pixel centres).

    Returns:
        Longitudes (from -180 degrees) and latitudes (south to north).

    """
    _, centred = recenter_longitudes(np.zeros((1, len(longitudes)), dtype=np.float32), longitudes)
    return centred[::DISPLAY_STRIDE], latitudes[::-DISPLAY_STRIDE]


def contour_segments(
    field: NDArray[np.float32],
    longitudes: NDArray[np.float64],
    latitudes: NDArray[np.float64],
    levels: tuple[float, ...],
) -> tuple[list[list[float]], list[list[float]]]:
    """Trace isolines of a displayed field, one polyline per connected piece.

    Returns:
        Longitude and latitude lists for a multi-line glyph.

    """
    generator = contourpy.contour_generator(longitudes, latitudes, field)
    xs: list[list[float]] = []
    ys: list[list[float]] = []
    for level in levels:
        for piece in generator.lines(level):
            line = np.asarray(piece, dtype=np.float64)
            xs.append(line[:, 0].tolist())
            ys.append(line[:, 1].tolist())
    return xs, ys


class AppModel:
    """State of the application for one run; the engine cache is dropped on a run change."""

    def __init__(
        self,
        output_root: Path,
        run_id: str | None = None,
        *,
        cache_leads: int = DEFAULT_CACHE_LEADS,
    ) -> None:
        """Open the run (the most recent one when ``run_id`` is None)."""
        self._output_root = output_root
        self._cache_leads = cache_leads
        self.run_id = ""
        self.initialization: datetime
        self.leads: tuple[int, ...] = ()
        self.selection: Selection
        self.display_longitudes: NDArray[np.float64]
        self.display_latitudes: NDArray[np.float64]
        self._engine: SensitivityEngine
        self.select_run(run_id)

    def run_labels(self) -> dict[str, str]:
        """List the indexed runs (purged of vanished directories) with readable labels.

        Returns:
            Label to run UUID, most recent first.

        """
        prune_index(self._output_root)
        runs = load_index(self._output_root)
        ordered = sorted(runs, key=lambda key: str(runs[key]["created_at_utc"]), reverse=True)
        return {f"{str(runs[key]['created_at_utc'])[:16]} | {key[:8]}": key for key in ordered}

    def select_run(self, run_id: str | None) -> None:
        """Switch to a run: reload its manifest and reset engine and selection."""
        self.run_id, run_dir = resolve_run(self._output_root, run_id)
        raw_dir = run_dir / RAW_FETCH_DIRNAME
        manifest = read_manifest(raw_dir)
        self.leads = manifest_leads(manifest)
        self.initialization = manifest_initialization(manifest)
        self.selection = default_selection(self.leads)
        latitudes, longitudes = grid_axes()
        self.display_longitudes, self.display_latitudes = display_axes(latitudes, longitudes)
        self._engine = SensitivityEngine(
            run_field_loader(raw_dir, manifest),
            latitudes,
            longitudes,
            MEMBERS,
            cache_leads=self._cache_leads,
        )

    def predictor_options(self, target_lead_hours: int) -> tuple[int, ...]:
        """List the leads strictly earlier than a target lead.

        Returns:
            Available predictor leads in increasing order.

        """
        return tuple(lead for lead in self.leads if lead < target_lead_hours)

    def update(self, **changes: float) -> Selection:
        """Apply validated changes; a new target keeps the predictor only if still earlier.

        Returns:
            The new selection (also stored).

        """
        candidate = replace(self.selection, **changes)
        if "target_lead_hours" in changes and "predictor_lead_hours" not in changes:
            options = self.predictor_options(candidate.target_lead_hours)
            if candidate.predictor_lead_hours not in options and options:
                candidate = replace(candidate, predictor_lead_hours=options[-1])
        candidate = replace(candidate, center_longitude=wrap_longitude(candidate.center_longitude))
        validate_selection(candidate, self.leads)
        self.selection = candidate
        return candidate

    def maps(self) -> MapsData:
        """Compute both maps for the current selection (on the fly, cached leads).

        Returns:
            Display-ready arrays and diagnostics.

        """
        selection = self.selection
        result = self._engine.sensitivity(
            target_lead_hours=selection.target_lead_hours,
            predictor_lead_hours=selection.predictor_lead_hours,
            center_latitude=selection.center_latitude,
            center_longitude=selection.center_longitude,
            radius_km=selection.radius_km,
        )
        stats = self._engine.ensemble_stats(selection.target_lead_hours)
        mean = to_display(stats.mean)
        outline = outline_with_breaks(
            selection.center_latitude, selection.center_longitude, selection.radius_km
        )
        return MapsData(
            correlation=to_display(result.correlation),
            mean=mean,
            std=to_display(stats.std),
            levels=contour_levels(mean),
            outline_longitudes=outline[0],
            outline_latitudes=outline[1],
            zone_cell_count=result.zone_cell_count,
            constant_points=result.constant_points,
            selection=selection,
        )
