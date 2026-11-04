# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render the signed correlation map and its JSON provenance."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, cast

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import numpy as np
from matplotlib.figure import Figure

from ensemble_sensitivity.field_io import recenter_longitudes
from ensemble_sensitivity.sensitivity.zone import EARTH_RADIUS_KM

if TYPE_CHECKING:
    from pathlib import Path

    from cartopy.mpl.geoaxes import GeoAxes
    from numpy.typing import NDArray

    from ensemble_sensitivity.sensitivity.compute import SensitivityResult

SOURCE_CREDIT = "Source : Météo-France"
CIRCLE_SAMPLES = 181


@dataclass(frozen=True, slots=True)
class SensitivityFigure:
    """Everything needed to draw one sensitivity map and describe where it came from."""

    result: SensitivityResult
    longitudes: NDArray[np.float64]
    latitudes: NDArray[np.float64]
    run_id: str | None
    coverage_id: object
    initialization: datetime
    center_latitude: float
    center_longitude: float
    radius_km: float


def zone_outline(
    center_latitude: float,
    center_longitude: float,
    radius_km: float,
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Return longitudes and latitudes (degrees) of the zone boundary, a great circle.

    Returns:
        Boundary points ordered by bearing, closed on the first point.

    """
    bearing = np.linspace(0.0, 2.0 * math.pi, CIRCLE_SAMPLES)
    angular = radius_km / EARTH_RADIUS_KM
    lat0 = math.radians(center_latitude)
    lon0 = math.radians(center_longitude)
    sin_lat = math.sin(lat0) * math.cos(angular) + math.cos(lat0) * math.sin(angular) * np.cos(
        bearing
    )
    lat = np.arcsin(np.clip(sin_lat, -1.0, 1.0))
    lon = lon0 + np.arctan2(
        np.sin(bearing) * math.sin(angular),
        math.cos(lat0) * math.cos(angular) - math.sin(lat0) * math.sin(angular) * np.cos(bearing),
    )
    return np.degrees(lon), np.degrees(lat)


def render_sensitivity(figure_data: SensitivityFigure, output_path: Path) -> None:
    """Write the PNG (atomically) and its JSON provenance next to it.

    Raises:
        ValueError: If the output path is not a ``.png`` or the map has no defined value.

    """
    if output_path.suffix.casefold() != ".png":
        raise ValueError(f"Sensitivity output path must use the .png suffix: {output_path}")
    correlation = figure_data.result.correlation
    if not np.any(np.isfinite(correlation)):
        raise ValueError("Correlation map has no defined value: nothing to draw.")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _draw_png(figure_data, output_path)
    _write_provenance(figure_data, output_path)


def _draw_png(data: SensitivityFigure, output_path: Path) -> None:
    result = data.result
    values, longitudes = recenter_longitudes(
        result.correlation.astype(np.float32),
        data.longitudes,
    )
    temporary_path = output_path.with_name(f"{output_path.stem}.tmp.png")
    geographic = ccrs.PlateCarree()
    figure = Figure(figsize=(13, 6.5), layout="constrained")
    axes = cast("GeoAxes", figure.add_subplot(1, 1, 1, projection=geographic))
    extent = (
        float(longitudes[0] - 0.125),
        float(longitudes[-1] + 0.125),
        float(data.latitudes[-1] - 0.125),
        float(data.latitudes[0] + 0.125),
    )
    image = axes.imshow(
        np.ma.masked_invalid(values),
        origin="upper",
        extent=extent,
        transform=geographic,
        cmap="RdBu_r",
        vmin=-1.0,
        vmax=1.0,
        interpolation="nearest",
        aspect="auto",
        zorder=2,
    )
    axes.add_feature(cfeature.LAND, facecolor="#f2efe7", zorder=1)
    axes.coastlines(resolution="110m", linewidth=0.6, color="#333333", zorder=3)
    axes.set_global()
    outline_lon, outline_lat = zone_outline(
        data.center_latitude,
        data.center_longitude,
        data.radius_km,
    )
    axes.plot(
        outline_lon,
        outline_lat,
        color="black",
        linewidth=1.6,
        transform=ccrs.Geodetic(),
        zorder=4,
    )
    axes.set_title(
        f"Sensibilité Z500 | run {data.initialization:%Y-%m-%d %H} UTC | "
        f"cible t+{result.target_lead_hours} h, prédicteur t+{result.predictor_lead_hours} h | "
        f"zone {data.center_latitude:.2f}, {data.center_longitude:.2f}, "
        f"{data.radius_km:g} km | {result.member_count} membres"
    )
    figure.colorbar(image, ax=axes, label="Corrélation de Pearson (signée)")
    figure.text(0.01, 0.01, SOURCE_CREDIT, ha="left", va="bottom")
    figure.savefig(temporary_path, dpi=150, bbox_inches="tight")
    temporary_path.replace(output_path)


def _write_provenance(data: SensitivityFigure, output_path: Path) -> None:
    result = data.result
    sidecar = {
        "created_utc": datetime.now(UTC).isoformat(),
        "run_id": data.run_id,
        "coverage_id": data.coverage_id,
        "initialization_utc": data.initialization.isoformat(),
        "target_lead_hours": result.target_lead_hours,
        "predictor_lead_hours": result.predictor_lead_hours,
        "center_latitude": data.center_latitude,
        "center_longitude": data.center_longitude,
        "radius_km": data.radius_km,
        "member_count": result.member_count,
        "zone_cell_count": result.zone_cell_count,
        "constant_points": result.constant_points,
        "target_minimum": float(np.min(result.target_values)),
        "target_maximum": float(np.max(result.target_values)),
        "correlation_minimum": float(np.nanmin(result.correlation)),
        "correlation_maximum": float(np.nanmax(result.correlation)),
        "method": "Pearson, signed, no significance test",
        "source_credit": SOURCE_CREDIT,
        "projection": "equirectangular",
        "output_sha256": hashlib.sha256(output_path.read_bytes()).hexdigest(),
    }
    sidecar_path = output_path.with_suffix(".json")
    temporary = sidecar_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(sidecar, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(sidecar_path)
