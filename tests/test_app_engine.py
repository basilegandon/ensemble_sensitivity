# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests of the on-the-fly sensitivity engine (synthetic fields, no browser)."""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pytest

from ensemble_sensitivity.app.engine import SensitivityEngine
from ensemble_sensitivity.sensitivity.compute import compute_sensitivity
from ensemble_sensitivity.sensitivity.statistics import StatisticsError

if TYPE_CHECKING:
    from numpy.typing import NDArray

MEMBERS = tuple(range(35))
LATITUDES = np.linspace(90.0, -90.0, 37)
LONGITUDES = np.arange(72, dtype=np.float64) * 5.0


def _fields(leads: tuple[int, ...]) -> dict[int, NDArray[np.float32]]:
    rng = np.random.default_rng(7)
    return {
        lead: (54_000.0 + 80.0 * rng.standard_normal((len(MEMBERS), 37, 72))).astype(np.float32)
        for lead in leads
    }


class _Loader:
    def __init__(self, fields: dict[int, NDArray[np.float32]]) -> None:
        self.fields = fields
        self.calls: list[tuple[int, int]] = []

    def __call__(self, member: int, lead: int) -> NDArray[np.float32]:
        self.calls.append((member, lead))
        return self.fields[lead][member]


def _engine(loader: _Loader, **kwargs: int) -> SensitivityEngine:
    return SensitivityEngine(loader, LATITUDES, LONGITUDES, MEMBERS, **kwargs)


def test_engine_matches_the_streaming_reference_and_correlates_the_target_with_itself() -> None:
    fields = _fields((24, 48))
    # Make the predictor field partly follow the zone mean so the correlation is not noise.
    fields[48] += 3.0 * fields[24][:, 10:11, 20:21]
    loader = _Loader(fields)
    zone = {"center_latitude": 40.0, "center_longitude": 100.0, "radius_km": 1500.0}
    result = _engine(loader).sensitivity(target_lead_hours=48, predictor_lead_hours=24, **zone)
    reference = compute_sensitivity(
        lambda member, lead: fields[lead][member],
        LATITUDES,
        LONGITUDES,
        members=MEMBERS,
        target_lead_hours=48,
        predictor_lead_hours=24,
        **zone,
    )
    np.testing.assert_allclose(result.correlation, reference.correlation, atol=2e-5)
    np.testing.assert_allclose(result.target_values, reference.target_values, rtol=1e-6)
    assert result.zone_cell_count == reference.zone_cell_count
    assert result.constant_points == 0
    assert np.nanmax(np.abs(result.correlation)) <= 1.0


def test_ensemble_stats_match_numpy() -> None:
    fields = _fields((24,))
    stats = _engine(_Loader(fields)).ensemble_stats(24)
    np.testing.assert_allclose(stats.mean, fields[24].mean(axis=0), rtol=1e-6)
    np.testing.assert_allclose(stats.std, fields[24].std(axis=0, ddof=1), rtol=1e-4)


def test_constant_predictor_points_are_nan_not_plausible_values() -> None:
    fields = _fields((24, 48))
    fields[24][:, 5, 5] = 54_321.0
    result = _engine(_Loader(fields)).sensitivity(
        target_lead_hours=48,
        predictor_lead_hours=24,
        center_latitude=0.0,
        center_longitude=0.0,
        radius_km=1000.0,
    )
    assert np.isnan(result.correlation[5, 5])
    assert result.constant_points == 1
    assert np.isfinite(result.correlation).sum() == result.correlation.size - 1


def test_zero_variance_target_is_an_explicit_error() -> None:
    fields = _fields((24, 48))
    fields[48][:] = 54_000.0
    engine = _engine(_Loader(fields))
    with pytest.raises(StatisticsError, match="zero variance"):
        engine.sensitivity(
            target_lead_hours=48,
            predictor_lead_hours=24,
            center_latitude=0.0,
            center_longitude=0.0,
            radius_km=1000.0,
        )


def test_spatially_cancelling_constant_target_is_not_float32_rounding_noise() -> None:
    fields = _fields((24, 48))
    member = np.arange(len(MEMBERS), dtype=np.float64)
    second = member * member % 17
    # Raw zone mean is exactly 54000 for every member although each cell varies.
    cells = np.stack([54_000.0 + member, 54_000.0 + second, 54_000.0 - member - second], axis=1)
    fields[48][:] = 54_000.0
    fields[48][:, 18, 35:38] = cells.astype(np.float32)
    engine = _engine(_Loader(fields))
    with pytest.raises(StatisticsError, match="zero variance"):
        engine.sensitivity(
            target_lead_hours=48,
            predictor_lead_hours=24,
            center_latitude=0.0,
            center_longitude=180.0,
            radius_km=600.0,
        )


def test_cache_is_bounded_reused_and_never_evicts_the_pair_in_use() -> None:
    fields = _fields((24, 48, 72, 96))
    loader = _Loader(fields)
    engine = _engine(loader)
    zone = {"center_latitude": 0.0, "center_longitude": 0.0, "radius_km": 1000.0}

    engine.sensitivity(target_lead_hours=96, predictor_lead_hours=24, **zone)
    assert set(engine.cached_leads) == {96, 24}
    loads = len(loader.calls)
    engine.sensitivity(target_lead_hours=96, predictor_lead_hours=24, **zone)
    assert len(loader.calls) == loads

    engine.sensitivity(target_lead_hours=96, predictor_lead_hours=48, **zone)
    assert set(engine.cached_leads) == {96, 48}
    engine.sensitivity(target_lead_hours=72, predictor_lead_hours=48, **zone)
    assert set(engine.cached_leads) == {72, 48}
    engine.sensitivity(target_lead_hours=96, predictor_lead_hours=72, **zone)
    assert set(engine.cached_leads) == {96, 72}
    assert len(engine.cached_leads) <= 2


def test_engine_rejects_too_few_members_and_a_too_small_cache() -> None:
    loader = _Loader(_fields((24,)))
    with pytest.raises(StatisticsError, match="members"):
        SensitivityEngine(loader, LATITUDES, LONGITUDES, (0, 1))
    with pytest.raises(StatisticsError, match="cache"):
        _engine(loader, cache_leads=1)


def test_invalid_zone_is_rejected_before_any_field_is_loaded() -> None:
    loader = _Loader(_fields((24, 48)))
    engine = _engine(loader)
    with pytest.raises(ValueError, match="radius"):
        engine.sensitivity(
            target_lead_hours=48,
            predictor_lead_hours=24,
            center_latitude=0.0,
            center_longitude=0.0,
            radius_km=0.0,
        )
    assert loader.calls == []
