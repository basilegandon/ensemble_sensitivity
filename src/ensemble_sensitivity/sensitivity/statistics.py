# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Ensemble statistics: weighted zone mean and signed Pearson correlation map."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from collections.abc import Iterable

    from numpy.typing import NDArray

MIN_MEMBERS = 3


class StatisticsError(ValueError):
    """Ensemble statistics cannot be computed reliably from the given data."""


@dataclass(frozen=True, slots=True)
class PearsonMap:
    """Signed Pearson correlation of every grid point with a scalar target.

    ``correlation`` is NaN at grid points whose ensemble values are all identical
    (zero variance), where the coefficient is undefined; ``constant_points`` counts them.
    """

    correlation: NDArray[np.float64]
    constant_points: int


def require_finite(field: NDArray[np.floating], *, label: str) -> None:
    """Reject a field containing NaN or infinite values.

    Raises:
        StatisticsError: If any value is not finite.

    """
    if not np.all(np.isfinite(field)):
        raise StatisticsError(f"Non-finite values in {label}.")


def weighted_mean(field: NDArray[np.floating], weights: NDArray[np.float64]) -> float:
    """Return the weighted mean of ``field`` over the cells where ``weights`` is positive.

    Only zone cells are inspected, so non-finite values outside the zone are ignored.

    Returns:
        ``sum(w * x) / sum(w)`` over the zone, in float64.

    Raises:
        StatisticsError: If shapes differ, the zone is empty, or a zone value is not finite.

    """
    if field.shape != weights.shape:
        raise StatisticsError(f"Field shape {field.shape} differs from weights {weights.shape}.")
    zone = weights > 0.0
    if not np.any(zone):
        raise StatisticsError("Weights select no grid cell.")
    values = field[zone].astype(np.float64)
    require_finite(values, label="the target zone")
    zone_weights = weights[zone]
    return float(np.dot(values, zone_weights) / zone_weights.sum())


def pearson_map(fields: Iterable[NDArray[np.floating]], target: NDArray[np.float64]) -> PearsonMap:
    """Correlate each grid point with ``target`` across members, one field at a time.

    ``fields`` yields one ``(lat, lon)`` field per member, in the same order as ``target``.
    Only a handful of grid-sized float64 accumulators are kept, never the full ensemble.

    Returns:
        The signed correlation map and the count of undefined (constant) points.

    A `StatisticsError` is raised if members are too few or mismatched, the target is
    constant, or a field has non-finite values or an inconsistent shape.

    """
    member_count = target.size
    _validate_target(target)
    centered = target - target.mean()
    sums = _accumulate(fields, centered)
    return sums.finish(member_count, float(np.dot(centered, centered)))


def _validate_target(target: NDArray[np.float64]) -> None:
    if target.size < MIN_MEMBERS:
        raise StatisticsError(f"At least {MIN_MEMBERS} members are required: {target.size}.")
    require_finite(target, label="the target values")
    if np.ptp(target) == 0.0:
        raise StatisticsError("Target has zero variance across members: correlation undefined.")


def _accumulate(fields: Iterable[NDArray[np.floating]], centered: NDArray[np.float64]) -> _Sums:
    member_count = centered.size
    iterator = iter(fields)
    first = next(iterator, None)
    if first is None:
        raise StatisticsError(f"No field for {member_count} target values.")
    require_finite(first, label="member field #0")
    sums = _Sums(first.astype(np.float64))
    sums.add(first.astype(np.float64), float(centered[0]))
    count = 1
    for count, field in enumerate(iterator, start=2):
        if count > member_count:
            raise StatisticsError(f"More fields than the {member_count} target values.")
        require_finite(field, label=f"member field #{count - 1}")
        sums.add(field.astype(np.float64), float(centered[count - 1]))
    if count != member_count:
        raise StatisticsError(f"Got {count} fields for {member_count} target values.")
    return sums


class _Sums:
    """Streaming sums (shifted by the first field) for the Pearson map."""

    def __init__(self, first: NDArray[np.float64]) -> None:
        self._shift = first
        self._sum = np.zeros_like(first)
        self._sum_sq = np.zeros_like(first)
        self._cross = np.zeros_like(first)
        self._low = first.copy()
        self._high = first.copy()

    def add(self, values: NDArray[np.float64], centered_target: float) -> None:
        """Fold one member field into the sums.

        Raises:
            StatisticsError: If the field shape differs from the first field.

        """
        if values.shape != self._shift.shape:
            raise StatisticsError(f"Inconsistent field shape: {values.shape}.")
        shifted = values - self._shift
        self._sum += shifted
        self._sum_sq += shifted * shifted
        self._cross += shifted * centered_target
        np.minimum(self._low, values, out=self._low)
        np.maximum(self._high, values, out=self._high)

    def finish(self, member_count: int, target_ss: float) -> PearsonMap:
        """Return the correlation map; constant points are NaN.

        Returns:
            The map and the count of undefined points.

        """
        constant = self._low == self._high
        variance_ss = np.where(constant, 1.0, self._sum_sq - self._sum**2 / member_count)
        denominator = np.sqrt(np.maximum(variance_ss, np.finfo(np.float64).tiny) * target_ss)
        correlation = np.where(constant, np.nan, np.clip(self._cross / denominator, -1.0, 1.0))
        return PearsonMap(correlation=correlation, constant_points=int(constant.sum()))
