# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic deterministic tests for the sensitivity calculation."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING, Any

import numpy as np
import pytest

from ensemble_sensitivity.sensitivity.compute import compute_sensitivity
from ensemble_sensitivity.sensitivity.statistics import (
    StatisticsError,
    pearson_map,
    weighted_mean,
)
from ensemble_sensitivity.sensitivity.zone import (
    EARTH_RADIUS_KM,
    MAX_RADIUS_KM,
    ZoneError,
    circular_zone_weights,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import NDArray

LATITUDES: NDArray[np.float64] = np.linspace(90.0, -90.0, 37)  # 5 degree grid
LONGITUDES: NDArray[np.float64] = np.arange(72, dtype=np.float64) * 5.0
KM_PER_DEGREE = math.pi * EARTH_RADIUS_KM / 180.0


def _zone(lat: float, lon: float, radius_km: float) -> np.ndarray:
    return circular_zone_weights(
        LATITUDES,
        LONGITUDES,
        center_latitude=lat,
        center_longitude=lon,
        radius_km=radius_km,
    )


def test_weighted_mean_matches_direct_cell_loop() -> None:
    rng = np.random.default_rng(0)
    field = rng.normal(5500.0, 80.0, size=(37, 72)).astype(np.float32)
    weights = _zone(45.0, 10.0, 1500.0)

    total = 0.0
    weight_sum = 0.0
    for i, lat in enumerate(LATITUDES):
        for j, lon in enumerate(LONGITUDES):
            d = _great_circle_km(float(lat), float(lon), 45.0, 10.0)
            if d <= 1500.0:
                w = math.cos(math.radians(lat))
                total += w * float(field[i, j])
                weight_sum += w
    assert weighted_mean(field, weights) == pytest.approx(total / weight_sum, rel=1e-12)
    assert np.count_nonzero(weights) > 1


def _great_circle_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    cosine = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl)
    return EARTH_RADIUS_KM * math.acos(max(-1.0, min(1.0, cosine)))


def test_zone_wraps_across_the_antimeridian() -> None:
    weights = _zone(0.0, 180.0, 600.0)
    assert weights[:, 36].any()
    assert np.array_equal(weights, _zone(0.0, -180.0, 600.0))
    wrap = _zone(0.0, 359.0, 700.0)
    assert wrap[:, 0].any()
    assert wrap[:, 71].any()


def test_zone_at_the_pole_is_longitude_symmetric() -> None:
    weights = _zone(90.0, 123.0, 600.0)
    rows = np.flatnonzero(weights.any(axis=1))
    assert 0 in rows
    assert np.all(weights[0] == weights[0, 0])
    assert weights[0, 0] >= 0.0
    assert np.all(weights >= 0.0)
    assert weighted_mean(np.full((37, 72), 5.0), weights) == pytest.approx(5.0)


def test_zone_covering_the_globe_includes_every_cell_with_cos_weights() -> None:
    weights = _zone(0.0, 0.0, MAX_RADIUS_KM)
    expected = np.clip(np.cos(np.radians(LATITUDES)), 0.0, None)[:, None] * np.ones((1, 72))
    assert np.allclose(weights, expected)


@pytest.mark.parametrize(
    ("lat", "lon", "radius"),
    [
        (91.0, 0.0, 100.0),
        (float("nan"), 0.0, 100.0),
        (0.0, float("inf"), 100.0),
        (0.0, 0.0, 0.0),
        (0.0, 0.0, -5.0),
        (0.0, 0.0, 25000.0),
    ],
)
def test_invalid_zone_is_rejected(lat: float, lon: float, radius: float) -> None:
    with pytest.raises(ZoneError):
        _zone(lat, lon, radius)


def test_zone_between_grid_points_is_empty_error() -> None:
    with pytest.raises(ZoneError, match="No grid cell"):
        _zone(2.5, 2.5, 5.0)


def test_zone_outside_a_regional_grid_is_empty_error() -> None:
    latitudes = np.linspace(60.0, 40.0, 5)
    longitudes = np.linspace(-10.0, 10.0, 5)
    with pytest.raises(ZoneError):
        circular_zone_weights(
            latitudes,
            longitudes,
            center_latitude=-30.0,
            center_longitude=100.0,
            radius_km=500.0,
        )


def test_weighted_mean_rejects_bad_input() -> None:
    weights = _zone(0.0, 0.0, 800.0)
    field = np.zeros((37, 72))
    with pytest.raises(StatisticsError, match="shape"):
        weighted_mean(np.zeros((3, 3)), weights)
    with pytest.raises(StatisticsError, match="no grid cell"):
        weighted_mean(field, np.zeros_like(weights))
    inside = np.argwhere(weights > 0)[0]
    field[tuple(inside)] = np.nan
    with pytest.raises(StatisticsError, match="Non-finite"):
        weighted_mean(field, weights)


def test_weighted_mean_ignores_non_finite_outside_zone() -> None:
    weights = _zone(0.0, 0.0, 800.0)
    field = np.ones((37, 72))
    field[0, 0] = np.nan
    assert weighted_mean(field, weights) == pytest.approx(1.0)


def _ensemble(n: int = 8) -> tuple[np.ndarray, np.ndarray]:
    rng = np.random.default_rng(42)
    fields = rng.normal(size=(n, 4, 6))
    target = rng.normal(size=n)
    return fields, target


def test_pearson_matches_numpy_corrcoef_and_is_signed() -> None:
    fields, target = _ensemble()
    fields[:, 0, 0] = 2.0 * target + 7.0
    fields[:, 1, 1] = -3.0 * target + 1.0
    result = pearson_map(iter(fields), target)

    assert result.constant_points == 0
    for i in range(4):
        for j in range(6):
            expected = np.corrcoef(fields[:, i, j], target)[0, 1]
            assert result.correlation[i, j] == pytest.approx(expected, abs=1e-12)
    assert result.correlation[0, 0] == pytest.approx(1.0)
    assert result.correlation[1, 1] == pytest.approx(-1.0)


def test_pearson_is_stable_with_large_offsets() -> None:
    fields, target = _ensemble()
    shifted = fields + 5.5e4
    expected = pearson_map(iter(fields), target).correlation
    assert np.allclose(pearson_map(iter(shifted), target).correlation, expected, atol=1e-9)


def test_pearson_zero_variance_target_is_an_error() -> None:
    fields, _ = _ensemble()
    with pytest.raises(StatisticsError, match="zero variance"):
        pearson_map(iter(fields), np.full(8, 3.3))


def test_pearson_constant_points_are_nan_not_plausible_values() -> None:
    fields, target = _ensemble()
    fields[:, 2, 3] = 5500.1
    result = pearson_map(iter(fields), target)
    assert result.constant_points == 1
    assert math.isnan(result.correlation[2, 3])
    assert np.isfinite(np.delete(result.correlation.ravel(), 2 * 6 + 3)).all()


def test_pearson_rejects_member_count_mismatch_and_non_finite() -> None:
    fields, target = _ensemble()
    with pytest.raises(StatisticsError, match="Got 7 fields"):
        pearson_map(iter(fields[:-1]), target)
    with pytest.raises(StatisticsError, match="More fields"):
        pearson_map(iter(np.concatenate([fields, fields[:1]])), target)
    with pytest.raises(StatisticsError, match="No field"):
        pearson_map(iter(()), target)
    with pytest.raises(StatisticsError, match="At least"):
        pearson_map(iter(fields[:2]), target[:2])
    bad = fields.copy()
    bad[3, 0, 0] = np.inf
    with pytest.raises(StatisticsError, match="Non-finite"):
        pearson_map(iter(bad), target)
    with pytest.raises(StatisticsError, match="Non-finite"):
        pearson_map(iter(fields), np.append(target[:-1], np.nan))
    ragged = [*fields[:-1], np.zeros((2, 2))]
    with pytest.raises(StatisticsError, match="shape"):
        pearson_map(iter(ragged), target)


def _synthetic_loader(n: int) -> tuple[Callable[[int, int], np.ndarray], np.ndarray]:
    rng = np.random.default_rng(7)
    base = rng.normal(5500.0, 50.0, size=(n, 37, 72))
    return (lambda member, lead: base[member] + lead), base


def test_compute_sensitivity_end_to_end_one_field_at_a_time() -> None:
    load, base = _synthetic_loader(10)
    calls: list[tuple[int, int]] = []

    def tracking(member: int, lead: int) -> np.ndarray:
        calls.append((member, lead))
        return load(member, lead)

    result = compute_sensitivity(
        tracking,
        LATITUDES,
        LONGITUDES,
        members=range(10),
        target_lead_hours=24,
        predictor_lead_hours=12,
        center_latitude=45.0,
        center_longitude=5.0,
        radius_km=1200.0,
    )
    weights = _zone(45.0, 5.0, 1200.0)
    expected_target = [weighted_mean(base[m] + 24, weights) for m in range(10)]
    assert np.allclose(result.target_values, expected_target)
    assert result.member_count == 10
    assert result.zone_cell_count == np.count_nonzero(weights)
    point = np.corrcoef(base[:, 10, 20], expected_target)[0, 1]
    assert result.correlation[10, 20] == pytest.approx(point, abs=1e-12)
    assert len(calls) == 20


def test_compute_sensitivity_reports_missing_member_and_bad_request() -> None:
    load, _ = _synthetic_loader(5)

    def missing(member: int, lead: int) -> np.ndarray:
        if member == 3:
            message = "member 3 missing"
            raise FileNotFoundError(message)
        return load(member, lead)

    kwargs: dict[str, Any] = {
        "target_lead_hours": 24,
        "predictor_lead_hours": 12,
        "center_latitude": 45.0,
        "center_longitude": 5.0,
        "radius_km": 1200.0,
    }
    with pytest.raises(FileNotFoundError, match="member 3"):
        compute_sensitivity(missing, LATITUDES, LONGITUDES, members=range(5), **kwargs)
    with pytest.raises(StatisticsError, match="Duplicate"):
        compute_sensitivity(load, LATITUDES, LONGITUDES, members=[0, 0, 1], **kwargs)
    with pytest.raises(StatisticsError, match="At least"):
        compute_sensitivity(load, LATITUDES, LONGITUDES, members=[0, 1], **kwargs)
    kwargs["predictor_lead_hours"] = 24
    with pytest.raises(StatisticsError, match="earlier"):
        compute_sensitivity(load, LATITUDES, LONGITUDES, members=range(5), **kwargs)
