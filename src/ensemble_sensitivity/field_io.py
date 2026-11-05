# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Read validated Z500 fields of a complete retrieval: manifest, GRIB file, decoding.

This module is the shared ingestion and decoding layer used by the quicklook maps and by
the sensitivity calculation. It knows nothing about plotting or statistics.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import numpy as np

from ensemble_sensitivity.data_retrieval.wcs_retrieval import (
    MAX_RESPONSE_BYTES,
    RetrievalError,
    _Eccodes,
    _load_eccodes,
    _validate_grib,
)

if TYPE_CHECKING:
    from numpy.typing import NDArray

GRID_NI = 1440
GRID_NJ = 721
SCAN_TOLERANCE = 1e-5
GRID_SCAN_SIGNATURE: dict[str, float] = {
    "latitudeOfFirstGridPointInDegrees": 90.0,
    "latitudeOfLastGridPointInDegrees": -90.0,
    "longitudeOfFirstGridPointInDegrees": 0.0,
    "longitudeOfLastGridPointInDegrees": 359.75,
    "iDirectionIncrementInDegrees": 0.25,
    "jDirectionIncrementInDegrees": 0.25,
    "iScansNegatively": 0,
    "jScansPositively": 0,
}


class ArrayEccodes(_Eccodes, Protocol):
    """ecCodes array access required to decode a field."""

    def codes_get_array(self, handle: object, key: str) -> object:
        """Read a GRIB array key."""


@dataclass(frozen=True, slots=True)
class DecodedField:
    """One decoded Z500 field in native grid order.

    ``values`` has shape ``(latitude, longitude)``; the latitude axis runs from 90 to -90
    degrees and the longitude axis from 0 to 359.75 degrees.
    """

    values: NDArray[np.float32]
    longitude_axis: NDArray[np.float64]
    latitude_axis: NDArray[np.float64]
    metadata: dict[str, int | float | str]


def read_manifest(run_dir: Path) -> dict[str, object]:
    """Read and validate the required complete-run manifest.

    Returns:
        Complete-run metadata and field inventory.

    Raises:
        RetrievalError: If the manifest is missing, malformed, or incomplete.

    """
    manifest_path = run_dir / "manifest.json"
    try:
        value = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RetrievalError(f"Cannot read retrieval manifest {manifest_path}: {error}") from error
    if not isinstance(value, dict):
        raise RetrievalError(f"Retrieval manifest is not a JSON object: {manifest_path}")
    manifest = cast("dict[str, object]", value)
    if manifest.get("status") != "complete":
        raise RetrievalError(f"Retrieval manifest is not complete: {manifest_path}")
    if not isinstance(manifest.get("fields"), list):
        raise RetrievalError(f"Retrieval manifest has no field inventory: {manifest_path}")
    return manifest


def grid_axes() -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Return the native PEARP grid axes (validated by :func:`decode_arrays`).

    Returns:
        Latitudes (north to south) and longitudes (0 to 359.75 degrees east).

    """
    return np.linspace(90.0, -90.0, GRID_NJ), np.arange(GRID_NI, dtype=np.float64) * 0.25


def manifest_leads(manifest: dict[str, object]) -> tuple[int, ...]:
    """List the forecast leads present in a complete manifest.

    Returns:
        Distinct lead hours in increasing order.

    Raises:
        RetrievalError: If the manifest has no field inventory.

    """
    fields = manifest["fields"]
    if not isinstance(fields, list):
        raise RetrievalError("Retrieval manifest has no field inventory")
    leads: set[int] = set()
    for value in fields:
        lead = value.get("lead_hours") if isinstance(value, dict) else None
        if isinstance(lead, int):
            leads.add(lead)
    return tuple(sorted(leads))


def find_field(
    manifest: dict[str, object],
    *,
    member: int,
    lead_hours: int,
) -> dict[str, object]:
    """Find the requested validated field in a complete manifest.

    Returns:
        Field provenance record.

    Raises:
        RetrievalError: If the complete manifest does not contain the field.

    """
    fields = manifest["fields"]
    if not isinstance(fields, list):
        raise RetrievalError("Retrieval manifest has no field inventory")
    for value in fields:
        if _field_matches(value, member=member, lead_hours=lead_hours):
            return cast("dict[str, object]", value)
    raise RetrievalError(
        f"Complete run manifest has no validated member {member:03}, lead {lead_hours} h"
    )


def _field_matches(value: object, *, member: int, lead_hours: int) -> bool:
    """Return whether a manifest entry identifies the requested field.

    Returns:
        Whether the entry matches the requested member and lead.

    """
    return (
        isinstance(value, dict)
        and value.get("member") == member
        and value.get("lead_hours") == lead_hours
    )


def manifest_field_path(run_dir: Path, field_record: dict[str, object]) -> Path:
    """Resolve a safe field filename from a manifest entry.

    Returns:
        The field path contained by the run directory.

    Raises:
        RetrievalError: If the manifest filename is invalid or escapes the run directory.

    """
    relative_path = field_record.get("relative_path")
    if not isinstance(relative_path, str) or Path(relative_path).name != relative_path:
        raise RetrievalError("Manifest field path must be a local filename")
    field_path = run_dir / relative_path
    if field_path.resolve().parent != run_dir.resolve():
        raise RetrievalError(f"Manifest field path escapes its run directory: {relative_path}")
    return field_path


def read_field_body(field_path: Path) -> bytes:
    """Read one bounded GRIB field from disk.

    Returns:
        The GRIB message bytes.

    Raises:
        RetrievalError: If the field exceeds the response size limit.

    """
    body = field_path.read_bytes()
    if len(body) > MAX_RESPONSE_BYTES:
        raise RetrievalError(f"Selected field exceeds {MAX_RESPONSE_BYTES} bytes: {field_path}")
    return body


def manifest_initialization(manifest: dict[str, object]) -> datetime:
    """Parse the timezone-aware run initialization from a manifest.

    Returns:
        The validated run initialization.

    Raises:
        RetrievalError: If the timestamp is absent, invalid, or timezone-naive.

    """
    value = manifest.get("initialization_utc")
    if not isinstance(value, str):
        raise RetrievalError("Retrieval manifest has no initialization timestamp")
    try:
        initialization = datetime.fromisoformat(value)
    except ValueError as error:
        message = "Retrieval manifest has an invalid initialization timestamp"
        raise RetrievalError(message) from error
    if initialization.tzinfo is None:
        raise RetrievalError("Retrieval initialization timestamp must include a timezone")
    return initialization


def _validate_grid_dimensions(ni: object, nj: object) -> tuple[int, int]:
    """Validate and return the documented global grid dimensions.

    Returns:
        The validated longitude and latitude dimensions.

    Raises:
        RetrievalError: If the dimensions do not match the global grid.

    """
    if not isinstance(ni, int) or not isinstance(nj, int) or (ni, nj) != (GRID_NI, GRID_NJ):
        raise RetrievalError(f"Unexpected Z500 grid dimensions: {ni} x {nj}")
    return ni, nj


def _validate_coordinate_axes(
    longitudes: NDArray[np.float64],
    latitudes: NDArray[np.float64],
) -> tuple[NDArray[np.float64], NDArray[np.float64]]:
    """Validate regular global-grid coordinates and return their axes.

    Returns:
        The longitude and latitude axes.

    Raises:
        RetrievalError: If coordinates are not on the documented global grid.

    """
    longitude_axis = longitudes[0, :]
    latitude_axis = latitudes[:, 0]
    if not np.allclose(longitudes, longitude_axis[None, :], atol=1e-5, rtol=0):
        raise RetrievalError("Z500 longitude coordinates are not a regular lat/lon grid")
    if not np.allclose(latitudes, latitude_axis[:, None], atol=1e-5, rtol=0):
        raise RetrievalError("Z500 latitude coordinates are not a regular lat/lon grid")
    _validate_axis_endpoints(longitude_axis, latitude_axis)
    _validate_axis_spacing(longitude_axis, latitude_axis)
    return longitude_axis, latitude_axis


def _validate_axis_endpoints(
    longitude_axis: NDArray[np.float64],
    latitude_axis: NDArray[np.float64],
) -> None:
    """Check that the global coordinate axes span the expected endpoints.

    Raises:
        RetrievalError: If either axis has unexpected endpoints.

    """
    if not np.allclose(longitude_axis[[0, -1]], [0.0, 359.75], atol=1e-5, rtol=0):
        raise RetrievalError("Z500 longitude axis does not span the documented global grid")
    if not np.allclose(latitude_axis[[0, -1]], [90.0, -90.0], atol=1e-5, rtol=0):
        raise RetrievalError("Z500 latitude axis does not span the documented global grid")


def _validate_axis_spacing(
    longitude_axis: NDArray[np.float64],
    latitude_axis: NDArray[np.float64],
) -> None:
    """Check that adjacent global-grid coordinates are spaced by 0.25 degrees.

    Raises:
        RetrievalError: If either axis has unexpected spacing.

    """
    if not np.allclose(np.diff(longitude_axis), 0.25, atol=1e-5, rtol=0):
        raise RetrievalError("Z500 longitude axis does not use 0.25-degree spacing")
    if not np.allclose(np.diff(latitude_axis), -0.25, atol=1e-5, rtol=0):
        raise RetrievalError("Z500 latitude axis does not use 0.25-degree spacing")


def _validate_scan_signature(handle: object, eccodes: ArrayEccodes) -> None:
    """Check the scalar grid definition (endpoints, increments, scanning order).

    Cheap enough to run on every message, unlike the coordinate arrays.

    Raises:
        RetrievalError: If the grid definition differs from the documented global grid.

    """
    for key, expected in GRID_SCAN_SIGNATURE.items():
        actual = eccodes.codes_get(handle, key)
        if (
            isinstance(actual, str)
            or not np.isfinite(actual)
            or abs(float(actual) - expected) > SCAN_TOLERANCE
        ):
            raise RetrievalError(
                f"Unexpected Z500 grid definition: {key}={actual!r}, expected {expected}"
            )


def _decode_values(handle: object, eccodes: ArrayEccodes) -> NDArray[np.float32]:
    """Decode the data values of an open message and check they are finite.

    Returns:
        Field values in native grid order.

    Raises:
        RetrievalError: If the grid is unexpected or values are non-finite.

    """
    ni, nj = _validate_grid_dimensions(
        eccodes.codes_get(handle, "Ni"),
        eccodes.codes_get(handle, "Nj"),
    )
    _validate_scan_signature(handle, eccodes)
    values = np.asarray(eccodes.codes_get_array(handle, "values"), dtype=np.float32).reshape(
        (nj, ni)
    )
    if not np.isfinite(values).all():
        raise RetrievalError("Z500 field contains non-finite values")
    return values


def decode_values(body: bytes, eccodes: ArrayEccodes) -> NDArray[np.float32]:
    """Decode one field without reading its coordinate arrays.

    The coordinate arrays dominate the decoding time and are identical for every field of
    a run: validate them once with :func:`decode_arrays`, then use this fast path.

    Returns:
        Field values in native grid order.

    """
    handle = eccodes.codes_new_from_message(body)
    try:
        return _decode_values(handle, eccodes)
    finally:
        eccodes.codes_release(handle)


def decode_arrays(
    body: bytes, eccodes: ArrayEccodes
) -> tuple[
    NDArray[np.float32],
    NDArray[np.float64],
    NDArray[np.float64],
]:
    """Decode one field and check its documented global grid coordinates.

    Returns:
        Field values, longitude axis and latitude axis in native grid order.

    """
    handle = eccodes.codes_new_from_message(body)
    try:
        values = _decode_values(handle, eccodes)
        nj, ni = values.shape
        latitudes = np.asarray(
            eccodes.codes_get_array(handle, "latitudes"), dtype=np.float64
        ).reshape((nj, ni))
        longitudes = np.asarray(
            eccodes.codes_get_array(handle, "longitudes"), dtype=np.float64
        ).reshape((nj, ni))
        longitude_axis, latitude_axis = _validate_coordinate_axes(longitudes, latitudes)
        return values, longitude_axis, latitude_axis
    finally:
        eccodes.codes_release(handle)


def _read_validated_body(
    run_dir: Path,
    manifest: dict[str, object],
    *,
    member: int,
    lead_hours: int,
) -> tuple[bytes, dict[str, int | float | str]]:
    """Read one manifest field from disk and validate it against the manifest.

    Returns:
        The GRIB message bytes and its validated metadata.

    """
    field_record = find_field(manifest, member=member, lead_hours=lead_hours)
    body = read_field_body(manifest_field_path(run_dir, field_record))
    initialization = manifest_initialization(manifest)
    metadata = _validate_grib(
        body,
        _load_eccodes(),
        member=member,
        lead_hours=lead_hours,
        initialization=initialization.astimezone(UTC),
    )
    return body, metadata


def load_field(
    run_dir: Path,
    manifest: dict[str, object],
    *,
    member: int,
    lead_hours: int,
) -> DecodedField:
    """Read, validate against the manifest, and decode one member/lead field.

    Args:
        run_dir: Directory holding the GRIB files and ``manifest.json``.
        manifest: Complete-run manifest returned by :func:`read_manifest`.
        member: Ensemble member identifier.
        lead_hours: Forecast lead in hours.

    Returns:
        The decoded field in native grid order with its validated GRIB metadata.

    """
    body, metadata = _read_validated_body(run_dir, manifest, member=member, lead_hours=lead_hours)
    values, longitude_axis, latitude_axis = decode_arrays(
        body, cast("ArrayEccodes", _load_eccodes())
    )
    return DecodedField(values, longitude_axis, latitude_axis, metadata)


def load_values(
    run_dir: Path,
    manifest: dict[str, object],
    *,
    member: int,
    lead_hours: int,
) -> NDArray[np.float32]:
    """Like :func:`load_field` but without coordinate arrays (grid already validated).

    Returns:
        The field values in native grid order.

    """
    body, _ = _read_validated_body(run_dir, manifest, member=member, lead_hours=lead_hours)
    return decode_values(body, cast("ArrayEccodes", _load_eccodes()))


def recenter_longitudes(
    values: NDArray[np.float32],
    longitude_axis: NDArray[np.float64],
) -> tuple[NDArray[np.float32], NDArray[np.float64]]:
    """Reorder field columns and longitude axis around the central meridian.

    Returns:
        Reordered field values and matching longitude coordinates.

    """
    half_width = len(longitude_axis) // 2
    values = np.roll(values, shift=-half_width, axis=1)
    longitude_axis = np.concatenate(
        (longitude_axis[half_width:] - 360.0, longitude_axis[:half_width])
    )
    return values, longitude_axis
