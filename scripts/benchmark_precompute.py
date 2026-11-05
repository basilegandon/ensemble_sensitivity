# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure the cost of the sensitivity pre-computation on synthetic 1440x721 PEARP-like data.

No token or real data is needed: Z500 fields are synthetic (smooth large-scale waves plus
member noise), and a real GRIB2 message is built with ecCodes to time the decoding.
Run ``uv run python scripts/benchmark_precompute.py`` and read the Markdown tables on stdout.
"""

from __future__ import annotations

import importlib
import io
import sys
import tempfile
import time
import tracemalloc
import zlib
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

import numpy as np

from ensemble_sensitivity.field_io import (
    GRID_NI,
    GRID_NJ,
    ArrayEccodes,
    decode_arrays,
    decode_values,
    grid_axes,
)
from ensemble_sensitivity.sensitivity.compute import (
    SensitivityResult,
    correlate_with_target,
    zone_target_values,
)
from ensemble_sensitivity.sensitivity.render import SensitivityFigure, render_sensitivity
from ensemble_sensitivity.sensitivity.statistics import weighted_mean
from ensemble_sensitivity.sensitivity.zone import circular_zone_weights

if TYPE_CHECKING:
    from collections.abc import Callable

    from numpy.typing import NDArray

MEMBERS = tuple(range(35))
CENTER = (46.0, 2.0)
RADII_KM = (250.0, 500.0, 1000.0, 2000.0)
TABLE_RULE = "|---|---|"


def _write(text: str = "") -> None:
    sys.stdout.write(text + "\n")


def _timed(function: Callable[[], object], repeats: int = 3) -> float:
    """Return the best wall time in seconds over ``repeats`` calls."""
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        function()
        best = min(best, time.perf_counter() - start)
    return best


def synthetic_fields(count: int = len(MEMBERS)) -> list[NDArray[np.float32]]:
    """Build smooth member fields (waves plus noise) shaped like decoded Z500."""
    rng = np.random.default_rng(42)
    latitudes, longitudes = grid_axes()
    lat = np.radians(latitudes)[:, None]
    lon = np.radians(longitudes)[None, :]
    base = 5500.0 + 200.0 * np.sin(3 * lon) * np.cos(lat) ** 2 - 400.0 * np.sin(lat) ** 2
    return [
        (
            base
            + 40.0 * rng.standard_normal((1, 1)) * np.cos(2 * lon + m)
            + rng.normal(0, 5, base.shape)
        ).astype(np.float32)
        for m in range(count)
    ]


def build_grib(field: NDArray[np.float32]) -> bytes:
    """Encode ``field`` as a global regular lat/lon GRIB2 message with ecCodes."""
    eccodes = cast("Any", importlib.import_module("eccodes"))
    handle = eccodes.codes_grib_new_from_samples("regular_ll_sfc_grib2")
    try:
        for key, value in {
            "Ni": GRID_NI,
            "Nj": GRID_NJ,
            "latitudeOfFirstGridPointInDegrees": 90.0,
            "latitudeOfLastGridPointInDegrees": -90.0,
            "longitudeOfFirstGridPointInDegrees": 0.0,
            "longitudeOfLastGridPointInDegrees": 359.75,
            "iDirectionIncrementInDegrees": 0.25,
            "jDirectionIncrementInDegrees": 0.25,
            "packingType": "grid_simple",
            "bitsPerValue": 16,
        }.items():
            eccodes.codes_set(handle, key, value)
        eccodes.codes_set_values(handle, field.ravel().astype(np.float64))
        return bytes(eccodes.codes_get_message(handle))
    finally:
        eccodes.codes_release(handle)


def decode_cost(field: NDArray[np.float32]) -> None:
    """Time the shared decoder (values plus latitude/longitude arrays) and values alone."""
    eccodes = cast("Any", importlib.import_module("eccodes"))
    body = build_grib(field)
    full = _timed(lambda: decode_arrays(body, cast("ArrayEccodes", eccodes)))

    typed = cast("ArrayEccodes", eccodes)
    fast = _timed(lambda: decode_values(body, typed))
    tracemalloc.start()
    decode_arrays(body, typed)
    peak_full = tracemalloc.get_traced_memory()[1]
    tracemalloc.reset_peak()
    decode_values(body, typed)
    peak_fast = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()

    _write("| Décodage d'un champ | temps (s) |")
    _write(TABLE_RULE)
    _write(f"| `decode_arrays` (valeurs + lat/lon) | {full:.3f} |")
    _write(f"| `decode_values` (valeurs seules, chemin rapide) | {fast:.3f} |")
    peaks = f"{peak_full / 1e6:.0f} / {peak_fast / 1e6:.0f} Mo"
    _write(f"| pic alloué `decode_arrays` / `decode_values` | {peaks} |")
    _write(f"| taille GRIB2 (16 bits) | {len(body) / 1e6:.2f} Mo |")
    _write()


def compute_cost(fields: list[NDArray[np.float32]]) -> None:
    """Time target, streaming Pearson and the in-memory matrix product."""
    latitudes, longitudes = grid_axes()
    weights = circular_zone_weights(
        latitudes,
        longitudes,
        center_latitude=CENTER[0],
        center_longitude=CENTER[1],
        radius_km=500.0,
    )

    def load(member: int, _lead: int) -> NDArray[np.float32]:
        return fields[member]

    target = zone_target_values(load, weights, MEMBERS, 24)

    def streaming() -> object:
        return correlate_with_target(
            load, weights, MEMBERS, target, target_lead_hours=24, predictor_lead_hours=0
        )

    stack = np.stack(fields).reshape(len(fields), -1)

    def matmul() -> object:
        anomaly = stack - stack.mean(axis=0)
        centered = target - target.mean()
        cov = centered @ anomaly
        scale = np.sqrt((anomaly * anomaly).sum(axis=0) * (centered @ centered))
        return cov / scale

    centered_stack = stack - stack.mean(axis=0)
    norms = np.sqrt((centered_stack * centered_stack).sum(axis=0))

    def matvec_cached() -> object:
        centered = (target - target.mean()).astype(np.float32)
        return (centered @ centered_stack) / (norms * np.sqrt(centered @ centered))

    tracemalloc.start()
    streaming()
    peak = tracemalloc.get_traced_memory()[1]
    tracemalloc.stop()
    _write("| Calcul d'une carte (champs déjà en RAM) | temps (s) | pic alloué par le calcul |")
    _write("|---|---|---|")
    _write(
        f"| Pearson en flux (35 champs un par un) | {_timed(streaming):.3f} | {peak / 1e6:.0f} Mo |"
    )
    _write(f"| Produit matriciel (35×1M, anomalies recalculées) | {_timed(matmul):.3f} | - |")
    _write(f"| Produit matriciel, anomalies/normes en cache | {_timed(matvec_cached):.3f} | - |")
    _write()
    _write("| Cible par membre (35 champs, rayon) | temps (s) |")
    _write(TABLE_RULE)
    for radius in RADII_KM:
        zone = circular_zone_weights(
            latitudes,
            longitudes,
            center_latitude=CENTER[0],
            center_longitude=CENTER[1],
            radius_km=radius,
        )
        elapsed = _timed(lambda zone=zone: [weighted_mean(f, zone) for f in fields])
        _write(f"| {radius:.0f} km ({np.count_nonzero(zone)} mailles) | {elapsed:.3f} |")
    _write()
    zone_time = _timed(
        lambda: circular_zone_weights(
            latitudes,
            longitudes,
            center_latitude=CENTER[0],
            center_longitude=CENTER[1],
            radius_km=500.0,
        )
    )
    _write(f"Poids de zone (une fois par centre/rayon) : {zone_time:.3f} s")
    _write()


def storage_cost(fields: list[NDArray[np.float32]]) -> None:
    """Report the size of one stored correlation map under several encodings."""
    stack = np.stack(fields)
    target = stack[:, 300:340, 700:740].mean(axis=(1, 2))
    centered = target - target.mean()
    anomaly = stack - stack.mean(axis=0)
    corr = np.einsum("m,mij->ij", centered, anomaly) / np.sqrt(
        (centered @ centered) * (anomaly * anomaly).sum(axis=0)
    )
    corr32 = corr.astype(np.float32)

    def compressed(array: NDArray[Any]) -> int:
        return len(zlib.compress(array.tobytes(), 6))

    quantized = np.round(corr * 127.0).astype(np.int8)
    rows = {
        "float32 brut (1440x721)": corr32.nbytes,
        "float32 + zlib": compressed(corr32),
        "float16 + zlib": compressed(corr.astype(np.float16)),
        "int8 (pas 1/127) brut": quantized.nbytes,
        "int8 + zlib": compressed(quantized),
        "int8 + zlib, grille 0,5° (1 point sur 2)": compressed(quantized[::2, ::2]),
        "int8 + zlib, grille 1° (1 point sur 4)": compressed(quantized[::4, ::4]),
    }
    _write("| Carte stockée (une carte) | taille (Ko) |")
    _write(TABLE_RULE)
    for name, size in rows.items():
        _write(f"| {name} | {size / 1024:.0f} |")
    _write()
    buffer = io.BytesIO()
    np.savez_compressed(buffer, corr=quantized)
    _write(f"(np.savez_compressed int8 : {buffer.tell() / 1024:.0f} Ko)")
    _write()


def render_cost() -> None:
    """Time the PNG rendering of one full-grid map (cartopy, real Natural Earth data)."""
    latitudes, longitudes = grid_axes()
    lat = np.radians(latitudes)[:, None]
    lon = np.radians(longitudes)[None, :]
    corr = np.sin(2 * lat) * np.cos(3 * lon)
    result = SensitivityResult(
        correlation=corr,
        target_values=np.arange(35.0),
        constant_points=0,
        zone_cell_count=1455,
        member_count=35,
        target_lead_hours=24,
        predictor_lead_hours=0,
    )
    data = SensitivityFigure(
        result=result,
        longitudes=longitudes,
        latitudes=latitudes,
        run_id="benchmark",
        coverage_id=None,
        initialization=datetime(2026, 1, 1, tzinfo=UTC),
        center_latitude=CENTER[0],
        center_longitude=CENTER[1],
        radius_km=500.0,
    )
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "map.png"
        elapsed = _timed(lambda: render_sensitivity(data, path), repeats=2)
        size = path.stat().st_size
    _write(f"Rendu PNG d une carte pleine grille : {elapsed:.2f} s ({size / 1024:.0f} Ko)")
    _write()


def main() -> None:
    """Print the benchmark tables."""
    fields = synthetic_fields()
    _write("## Coût du précalcul (données synthétiques)")
    _write()
    decode_cost(fields[0])
    compute_cost(fields)
    storage_cost(fields)
    render_cost()


if __name__ == "__main__":
    main()
