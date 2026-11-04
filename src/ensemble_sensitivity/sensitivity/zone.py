# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Spatial selection: circular target zone and area weights on a latitude/longitude grid."""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from numpy.typing import NDArray

EARTH_RADIUS_KM = 6371.0088
MAX_RADIUS_KM = math.pi * EARTH_RADIUS_KM


class ZoneError(ValueError):
    """The target zone is invalid, outside the grid, or selects no grid cell."""


def circular_zone_weights(
    latitudes: NDArray[np.float64],
    longitudes: NDArray[np.float64],
    *,
    center_latitude: float,
    center_longitude: float,
    radius_km: float,
) -> NDArray[np.float64]:
    """Return cos(latitude) weights inside a circle and 0 outside, shape ``(lat, lon)``.

    Membership uses the great-circle (haversine) distance between the cell centre and the
    zone centre, so the zone wraps across the antimeridian and covers the poles correctly.
    Weights are proportional to the cell area on a regular latitude/longitude grid.

    Returns:
        Non-negative weights; strictly positive only for cells inside the zone.

    Raises:
        ZoneError: If an argument is invalid or no grid cell lies inside the zone.

    """
    _validate_zone(center_latitude, center_longitude, radius_km)
    lat = np.radians(latitudes)[:, np.newaxis]
    lon = np.radians(longitudes)[np.newaxis, :]
    centre_lat = math.radians(center_latitude)
    centre_lon = math.radians(center_longitude)
    haversine = (
        np.sin((lat - centre_lat) / 2.0) ** 2
        + np.cos(lat) * math.cos(centre_lat) * np.sin((lon - centre_lon) / 2.0) ** 2
    )
    distance_km = 2.0 * EARTH_RADIUS_KM * np.arcsin(np.sqrt(np.clip(haversine, 0.0, 1.0)))
    inside = distance_km <= radius_km
    weights = np.where(inside, np.clip(np.cos(lat), 0.0, None), 0.0)
    if not np.any(weights > 0.0):
        raise ZoneError(
            f"No grid cell lies within {radius_km} km of ({center_latitude}, {center_longitude}).",
        )
    return np.asarray(weights, dtype=np.float64)


def _validate_zone(center_latitude: float, center_longitude: float, radius_km: float) -> None:
    if not (math.isfinite(center_latitude) and -90.0 <= center_latitude <= 90.0):
        raise ZoneError(f"Zone centre latitude must be in [-90, 90]: {center_latitude}")
    if not math.isfinite(center_longitude):
        raise ZoneError(f"Zone centre longitude must be finite: {center_longitude}")
    _validate_radius(radius_km)


def _validate_radius(radius_km: float) -> None:
    if not (math.isfinite(radius_km) and 0.0 < radius_km <= MAX_RADIUS_KM):
        raise ZoneError(f"Zone radius must be in ]0, {MAX_RADIUS_KM:.0f}] km: {radius_km}")
