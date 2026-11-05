# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""On-the-fly sensitivity engine with a bounded per-lead cache (no UI, no I/O format).

A cached lead keeps the 35 member fields as float32 anomalies about the ensemble mean
(145 MB for the 1440x721 grid), plus the mean, the spread and the inverse anomaly norm. The
Pearson correlation is shift-invariant, so a zone target built from anomalies correlates
exactly like the raw one, and a map costs one matrix-vector product.
"""

from __future__ import annotations

import logging
from collections import OrderedDict
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

from ensemble_sensitivity.sensitivity.statistics import MIN_MEMBERS, StatisticsError
from ensemble_sensitivity.sensitivity.zone import circular_zone_weights

if TYPE_CHECKING:
    from collections.abc import Sequence

    from numpy.typing import NDArray

    from ensemble_sensitivity.sensitivity.compute import FieldLoader

LOGGER = logging.getLogger(__name__)

# Rows centred at once: bounds the float64 temporaries (35 x 40 x 1440 x 8 B = 16 MB).
ROW_BLOCK = 40
DEFAULT_CACHE_LEADS = 2


@dataclass(frozen=True, slots=True)
class EnsembleStats:
    """Ensemble mean and sample standard deviation (ddof=1) at one lead."""

    mean: NDArray[np.float32]
    std: NDArray[np.float32]


@dataclass(frozen=True, slots=True)
class SensitivityMap:
    """Signed correlation map for one zone, target lead and predictor lead.

    ``correlation`` is NaN where the predictor is identical across members (undefined).
    ``target_values`` are the per-member raw zone means at the target lead.
    """

    correlation: NDArray[np.float32]
    target_values: NDArray[np.float64]
    zone_cell_count: int
    constant_points: int


@dataclass(slots=True)
class _LeadEntry:
    anomalies: NDArray[np.float32]
    stats: EnsembleStats
    inverse_norm: NDArray[np.float32]
    constant: NDArray[np.bool_]


def _centre_in_place(stack: NDArray[np.float32]) -> _LeadEntry:
    """Turn a member stack into anomalies in place, row block by row block.

    Returns:
        The cache entry owning ``stack`` (now anomalies) and its statistics.

    """
    members, rows, columns = stack.shape
    mean = np.empty((rows, columns), dtype=np.float32)
    std = np.empty((rows, columns), dtype=np.float32)
    inverse_norm = np.empty((rows, columns), dtype=np.float32)
    constant = np.empty((rows, columns), dtype=np.bool_)
    for start in range(0, rows, ROW_BLOCK):
        window = slice(start, min(start + ROW_BLOCK, rows))
        block = stack[:, window, :].astype(np.float64)
        constant[window] = np.ptp(block, axis=0) <= 0.0
        mean[window] = block.mean(axis=0)
        block -= block.mean(axis=0)
        block[:, constant[window]] = 0.0
        sum_squares = np.einsum("mrc,mrc->rc", block, block)
        std[window] = np.sqrt(sum_squares / (members - 1))
        safe = np.where(constant[window], 1.0, sum_squares)
        inverse_norm[window] = np.where(constant[window], 0.0, 1.0 / np.sqrt(safe))
        stack[:, window, :] = block
    return _LeadEntry(stack, EnsembleStats(mean, std), inverse_norm, constant)


class SensitivityEngine:
    """Compute ensemble statistics and sensitivity maps from a field loader.

    At most ``cache_leads`` leads are kept (least recently used evicted first), so the
    memory is bounded by about ``cache_leads`` x 145 MB for the native grid.
    """

    def __init__(
        self,
        load: FieldLoader,
        latitudes: NDArray[np.float64],
        longitudes: NDArray[np.float64],
        members: Sequence[int],
        *,
        cache_leads: int = DEFAULT_CACHE_LEADS,
    ) -> None:
        """Store the loader and grid.

        Raises:
            StatisticsError: If there are too few members or the cache cannot hold two leads.

        """
        if len(members) < MIN_MEMBERS:
            raise StatisticsError(f"At least {MIN_MEMBERS} members are required: {len(members)}.")
        if cache_leads < DEFAULT_CACHE_LEADS:
            raise StatisticsError(f"The cache must hold at least {DEFAULT_CACHE_LEADS} leads.")
        self._load = load
        self._latitudes = latitudes
        self._longitudes = longitudes
        self._members = tuple(members)
        self._cache_leads = cache_leads
        self._entries: OrderedDict[int, _LeadEntry] = OrderedDict()

    @property
    def cached_leads(self) -> tuple[int, ...]:
        """Leads currently cached, least recently used first."""
        return tuple(self._entries)

    def ensemble_stats(self, lead_hours: int) -> EnsembleStats:
        """Return the ensemble mean and spread at ``lead_hours``.

        Returns:
            Mean and standard deviation fields in native grid order.

        """
        return self._entry(lead_hours, keep=()).stats

    def sensitivity(
        self,
        *,
        target_lead_hours: int,
        predictor_lead_hours: int,
        center_latitude: float,
        center_longitude: float,
        radius_km: float,
    ) -> SensitivityMap:
        """Correlate the zone-mean target with every point of the predictor field.

        Returns:
            The signed correlation map and its diagnostics.

        Raises:
            StatisticsError: If the target has zero variance across members.

        """
        weights = circular_zone_weights(
            self._latitudes,
            self._longitudes,
            center_latitude=center_latitude,
            center_longitude=center_longitude,
            radius_km=radius_km,
        )
        target = self._entry(target_lead_hours, keep=(predictor_lead_hours,))
        predictor = self._entry(predictor_lead_hours, keep=(target_lead_hours,))
        member_count = len(self._members)
        zone = np.flatnonzero(weights)
        zone_weights = weights.ravel()[zone]
        values = target.anomalies.reshape(member_count, -1)[:, zone].astype(np.float64)
        centred_target = values @ zone_weights / zone_weights.sum()
        # Anomalies are rounded to float32: each carries a relative error <= eps/2, so
        # target spreads below eps x max|anomaly| are rounding noise, not a signal.
        resolution = float(np.finfo(np.float32).eps) * float(np.abs(values).max(initial=0.0))
        if np.ptp(centred_target) <= resolution:
            raise StatisticsError("Target has zero variance across members: correlation undefined.")
        norm = np.float32(np.sqrt(centred_target @ centred_target))
        cross = centred_target.astype(np.float32) @ predictor.anomalies.reshape(member_count, -1)
        correlation = cross.reshape(predictor.constant.shape) * predictor.inverse_norm / norm
        np.clip(correlation, -1.0, 1.0, out=correlation)
        correlation[predictor.constant] = np.nan
        raw_offset = float(target.stats.mean.ravel()[zone] @ zone_weights / zone_weights.sum())
        return SensitivityMap(
            correlation=correlation,
            target_values=centred_target + raw_offset,
            zone_cell_count=int(zone.size),
            constant_points=int(predictor.constant.sum()),
        )

    def _entry(self, lead_hours: int, *, keep: tuple[int, ...]) -> _LeadEntry:
        """Return the cached lead (most recently used), loading it when absent.

        Returns:
            The cache entry of the lead.

        """
        entry = self._entries.get(lead_hours)
        if entry is not None:
            self._entries.move_to_end(lead_hours)
            return entry
        self._evict(keep)
        LOGGER.debug("Loading %d member fields at t+%d h", len(self._members), lead_hours)
        first = np.asarray(self._load(self._members[0], lead_hours), dtype=np.float32)
        stack = np.empty((len(self._members), *first.shape), dtype=np.float32)
        stack[0] = first
        for index, member in enumerate(self._members[1:], start=1):
            stack[index] = self._load(member, lead_hours)
        entry = _centre_in_place(stack)
        self._entries[lead_hours] = entry
        return entry

    def _evict(self, keep: tuple[int, ...]) -> None:
        """Make room for one more lead, never evicting the leads in ``keep``."""
        while len(self._entries) >= self._cache_leads:
            victim = next((lead for lead in self._entries if lead not in keep), None)
            if victim is None:
                return
            del self._entries[victim]
