# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic tests for the application model (selection logic and map data, no UI)."""

from __future__ import annotations

import math
from dataclasses import replace
from typing import TYPE_CHECKING

import numpy as np
import pytest

from ensemble_sensitivity.app import model
from ensemble_sensitivity.app.model import AppModel, AppSelectionError, Selection
from tests.app_support import INITIALIZATION, LEADS, make_run, patch_synthetic_grid

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def synthetic(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_synthetic_grid(monkeypatch)


def _selection(**changes: float) -> Selection:
    return replace(Selection(9, 6, 46.5, 2.5, 300.0), **changes)


def test_validate_selection_accepts_default_and_rejects_bad_values() -> None:
    model.validate_selection(_selection(), LEADS)
    cases = [
        (_selection(target_lead_hours=0, predictor_lead_hours=0), "Target lead"),
        (_selection(target_lead_hours=4), "Target lead"),
        (_selection(predictor_lead_hours=9), "Predictor lead"),
        (_selection(predictor_lead_hours=1), "Predictor lead"),
        (_selection(center_latitude=91.0), "Latitude"),
        (_selection(center_latitude=math.nan), "Latitude"),
        (_selection(center_longitude=math.inf), "Longitude"),
        (_selection(radius_km=model.RADIUS_MIN_KM - 1), "Radius"),
        (_selection(radius_km=model.RADIUS_MAX_KM + 1), "Radius"),
        (_selection(radius_km=math.nan), "Radius"),
    ]
    for selection, message in cases:
        with pytest.raises(AppSelectionError, match=message):
            model.validate_selection(selection, LEADS)


def test_default_selection_and_too_few_leads() -> None:
    assert model.default_selection(LEADS) == _selection(center_latitude=46.5)
    with pytest.raises(AppSelectionError, match="at least two"):
        model.default_selection((0,))


@pytest.mark.parametrize(
    ("longitude", "expected"),
    [(0.0, 0.0), (180.0, -180.0), (-180.0, -180.0), (190.0, -170.0), (540.0, -180.0)],
)
def test_wrap_longitude(longitude: float, expected: float) -> None:
    assert model.wrap_longitude(longitude) == pytest.approx(expected)


def test_contour_levels_are_round_and_cover_the_range() -> None:
    field = np.array([[5400.0, 5900.0]], dtype=np.float32)
    levels = model.contour_levels(field)
    steps = np.diff(levels)
    assert len(levels) >= model.MIN_LEVEL_COUNT
    assert np.allclose(steps, steps[0])
    assert levels[0] >= 5400.0
    assert levels[-1] <= 5900.0
    assert model.contour_levels(np.full((2, 2), 5.0, dtype=np.float32)) == ()


def test_outline_breaks_at_the_antimeridian() -> None:
    longitudes, latitudes = model.outline_with_breaks(0.0, 179.0, 500.0)
    assert np.isnan(longitudes).any()
    assert len(longitudes) == len(latitudes)
    assert np.nanmax(np.abs(longitudes)) <= 180.0
    plain, _ = model.outline_with_breaks(0.0, 0.0, 500.0)
    assert not np.isnan(plain).any()


def test_to_display_orients_south_up_and_recentres_on_zero() -> None:
    field = np.arange(8 * 8, dtype=np.float32).reshape(8, 8)
    shown = model.to_display(field)
    assert shown.shape == (4, 4)
    assert shown.flags.c_contiguous
    rolled = np.roll(field, -4, axis=1)
    assert np.array_equal(shown, rolled[::-2, ::2])


@pytest.mark.usefixtures("synthetic")
def test_model_lists_runs_and_purges_vanished_directories(tmp_path: Path) -> None:
    old = make_run(tmp_path, day=1)
    recent = make_run(tmp_path, day=2)
    gone = make_run(tmp_path, day=3)
    (tmp_path / gone / "raw_fetch" / "manifest.json").unlink()
    (tmp_path / gone / "raw_fetch").rmdir()
    (tmp_path / gone).rmdir()
    app = AppModel(tmp_path)
    labels = app.run_labels()
    assert list(labels.values()) == [recent, old]
    assert app.run_id == recent
    assert app.leads == LEADS
    assert app.initialization == INITIALIZATION


@pytest.mark.usefixtures("synthetic")
def test_update_adjusts_predictor_and_wraps_longitude(tmp_path: Path) -> None:
    make_run(tmp_path)
    app = AppModel(tmp_path)
    updated = app.update(target_lead_hours=3)
    assert (updated.target_lead_hours, updated.predictor_lead_hours) == (3, 0)
    assert app.update(center_longitude=190.0).center_longitude == pytest.approx(-170.0)
    before = app.selection
    with pytest.raises(AppSelectionError, match="Radius"):
        app.update(radius_km=1.0)
    assert app.selection == before


@pytest.mark.usefixtures("synthetic")
def test_maps_returns_display_arrays_and_diagnostics(tmp_path: Path) -> None:
    make_run(tmp_path)
    app = AppModel(tmp_path)
    app.update(center_latitude=50.0, center_longitude=-10.0, radius_km=400.0)
    data = app.maps()
    assert data.correlation.shape == data.mean.shape == data.std.shape == (11, 10)
    assert data.zone_cell_count > 0
    assert np.nanmax(np.abs(data.correlation)) <= 1.0
    assert np.all(data.std[np.isfinite(data.std)] >= 0.0)
    assert len(data.levels) >= model.MIN_LEVEL_COUNT
    assert data.selection == app.selection
    assert len(data.outline_longitudes) == len(data.outline_latitudes)


@pytest.mark.usefixtures("synthetic")
def test_select_run_resets_selection(tmp_path: Path) -> None:
    first = make_run(tmp_path, day=1, leads=(0, 3))
    make_run(tmp_path, day=2)
    app = AppModel(tmp_path)
    app.update(target_lead_hours=3)
    app.select_run(first)
    assert app.run_id == first
    assert app.selection.target_lead_hours == 3
    assert app.selection.predictor_lead_hours == 0
