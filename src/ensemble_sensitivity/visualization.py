# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Render standalone Z500 quicklook maps from a completed retrieval."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from itertools import product
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import cartopy.crs as ccrs
import cartopy.feature as cfeature
import matplotlib.pyplot as plt
import numpy as np
from tqdm import tqdm

from ensemble_sensitivity.data_retrieval.wcs_retrieval import (
    MAX_RESPONSE_BYTES,
    RetrievalError,
    _Eccodes,
    _load_eccodes,
    _validate_grib,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from cartopy.mpl.geoaxes import GeoAxes
    from numpy.typing import NDArray

logger = logging.getLogger(__name__)
FIELD_TITLE = "PEARP Z500 quicklook"
SOURCE_CREDIT = "Source : Météo-France"


@dataclass(slots=True)
class _QuicklookContext:
    """Selected, validated field and output metadata for a quicklook."""

    run_dir: Path
    output_path: Path
    manifest: dict[str, object]
    initialization: datetime
    member: int
    lead_hours: int
    metadata: dict[str, int | float | str]
    values: NDArray[np.float32]
    longitude_axis: NDArray[np.float64]
    latitude_axis: NDArray[np.float64]


class _ArrayEccodes(_Eccodes, Protocol):
    """ecCodes array access required to decode a field for plotting."""

    def codes_get_array(self, handle: object, key: str) -> object:
        """Read a GRIB array key."""


def _read_manifest(run_dir: Path) -> dict[str, object]:
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


def _find_field(
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


def _validate_grid_dimensions(ni: object, nj: object) -> tuple[int, int]:
    """Validate and return the documented global grid dimensions.

    Returns:
        The validated longitude and latitude dimensions.

    Raises:
        RetrievalError: If the dimensions do not match the global grid.

    """
    if not isinstance(ni, int) or not isinstance(nj, int) or (ni, nj) != (1440, 721):
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


def _decode_arrays(
    body: bytes, eccodes: _ArrayEccodes
) -> tuple[
    NDArray[np.float32],
    NDArray[np.float64],
    NDArray[np.float64],
]:
    """Decode one field and check its documented global grid coordinates.

    Returns:
        Field values, longitudes, and latitudes in grid order.

    Raises:
        RetrievalError: If the grid is unexpected or values are non-finite.

    """
    handle = eccodes.codes_new_from_message(body)
    try:
        ni, nj = _validate_grid_dimensions(
            eccodes.codes_get(handle, "Ni"),
            eccodes.codes_get(handle, "Nj"),
        )
        values = np.asarray(eccodes.codes_get_array(handle, "values"), dtype=np.float32).reshape(
            (nj, ni)
        )
        latitudes = np.asarray(
            eccodes.codes_get_array(handle, "latitudes"), dtype=np.float64
        ).reshape((nj, ni))
        longitudes = np.asarray(
            eccodes.codes_get_array(handle, "longitudes"), dtype=np.float64
        ).reshape((nj, ni))
        if not np.isfinite(values).all():
            raise RetrievalError("Z500 field contains non-finite values")
        longitude_axis, latitude_axis = _validate_coordinate_axes(longitudes, latitudes)
        values, longitude_axis = _recenter_longitudes(values, longitude_axis)
        return values, longitude_axis, latitude_axis
    finally:
        eccodes.codes_release(handle)


def _recenter_longitudes(
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


def _load_quicklook_context(
    run_dir: Path,
    *,
    lead_hours: int,
    member: int,
    output_path: Path | None = None,
) -> _QuicklookContext:
    """Load one manifest-validated field and its plotting metadata.

    Returns:
        Quicklook data and provenance ready for rendering.

    """
    manifest = _read_manifest(run_dir)
    field_record = _find_field(manifest, member=member, lead_hours=lead_hours)
    field_path = _manifest_field_path(run_dir, field_record)
    body = _read_field_body(field_path)
    initialization = _manifest_initialization(manifest)
    eccodes = _load_eccodes()
    metadata = _validate_grib(
        body,
        eccodes,
        member=member,
        lead_hours=lead_hours,
        initialization=initialization.astimezone(UTC),
    )
    values, longitude_axis, latitude_axis = _decode_arrays(body, cast("_ArrayEccodes", eccodes))
    output_path = _quicklook_output_path(run_dir, member, lead_hours, output_path)
    return _QuicklookContext(
        run_dir=run_dir,
        output_path=output_path,
        manifest=manifest,
        initialization=initialization,
        member=member,
        lead_hours=lead_hours,
        metadata=metadata,
        values=values,
        longitude_axis=longitude_axis,
        latitude_axis=latitude_axis,
    )


def _manifest_field_path(run_dir: Path, field_record: dict[str, object]) -> Path:
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


def _read_field_body(field_path: Path) -> bytes:
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


def _manifest_initialization(manifest: dict[str, object]) -> datetime:
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


def _quicklook_output_path(
    run_dir: Path,
    member: int,
    lead_hours: int,
    output_path: Path | None,
) -> Path:
    """Select the default PNG path or validate the requested path.

    Returns:
        The requested or default output path.

    Raises:
        ValueError: If the requested output path does not use a PNG suffix.

    """
    if output_path is None:
        return run_dir / f"z500_member_{member:03}_lead_{lead_hours:03}.png"
    if output_path.suffix.casefold() != ".png":
        raise ValueError("Quicklook output path must use the .png suffix")
    return output_path


def _render_png(context: _QuicklookContext) -> None:
    """Save a global equirectangular PNG for the selected field."""
    output_path = context.output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f"{output_path.stem}.tmp.png")
    geographic_crs = ccrs.PlateCarree()
    figure = plt.figure(figsize=(13, 6.5), constrained_layout=True)
    try:
        axes = cast("GeoAxes", figure.add_subplot(1, 1, 1, projection=ccrs.PlateCarree()))
        extent = (
            float(context.longitude_axis[0] - 0.125),
            float(context.longitude_axis[-1] + 0.125),
            float(context.latitude_axis[-1] - 0.125),
            float(context.latitude_axis[0] + 0.125),
        )
        image = axes.imshow(
            context.values,
            origin="upper",
            extent=extent,
            transform=geographic_crs,
            alpha=0.82,
            cmap="viridis",
            interpolation="nearest",
            aspect="auto",
            zorder=2,
        )
        axes.add_feature(cfeature.OCEAN, facecolor="#dcecf5", zorder=0)
        axes.add_feature(cfeature.LAND, facecolor="#f2efe7", zorder=1)
        axes.coastlines(resolution="110m", linewidth=0.6, color="#333333", zorder=3)
        axes.add_feature(
            cfeature.BORDERS.with_scale("110m"),
            edgecolor="#555555",
            linewidth=0.35,
            zorder=3,
        )
        axes.set_global()
        axes.gridlines(
            crs=geographic_crs,
            draw_labels=False,
            linewidth=0.35,
            color="#666666",
            alpha=0.45,
            linestyle=":",
        )
        axes.set_title(
            f"{FIELD_TITLE} | run {context.initialization:%Y-%m-%d %H} UTC | "
            f"t+{context.lead_hours} h | member {context.member:03}"
        )
        axes.set_xlabel("Longitude (degrees east)")
        axes.set_ylabel("Latitude (degrees north)")
        figure.colorbar(image, ax=axes, label="Geopotential (m^2 s^-2)")
        figure.text(0.01, 0.01, SOURCE_CREDIT, ha="left", va="bottom")
        figure.savefig(temporary_path, dpi=150, bbox_inches="tight")
    finally:
        plt.close(figure)
    temporary_path.replace(output_path)


def _parse_selection(
    value: str | Sequence[int],
    candidates: Sequence[int],
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> tuple[int, ...]:
    """Parse a comma-separated or wildcard selection and validate its domain.

    Returns:
        Selected values in request order.

    Raises:
        ValueError: If the selection is malformed, empty, duplicated, or out of range.

    """
    selected = _selection_values(value, candidates, label)
    if not selected:
        raise ValueError(f"{label} selection cannot be empty")
    _validate_selection_values(
        selected,
        candidates,
        label=label,
        minimum=minimum,
        maximum=maximum,
    )
    return selected


def _selection_values(
    value: str | Sequence[int],
    candidates: Sequence[int],
    label: str,
) -> tuple[int, ...]:
    """Expand wildcard or comma-separated selections into integer values.

    Returns:
        Parsed selection in request order.

    """
    if not isinstance(value, str):
        return tuple(value)
    tokens = [token.strip() for token in value.split(",")]
    return _expand_selection_tokens(tokens, candidates, label)


def _expand_selection_tokens(
    tokens: list[str],
    candidates: Sequence[int],
    label: str,
) -> tuple[int, ...]:
    """Expand wildcard tokens or parse explicit integer tokens.

    Returns:
        Expanded wildcard candidates or parsed explicit values.

    Raises:
        ValueError: If wildcard selection is combined with explicit values.

    """
    if tokens == ["*"]:
        return tuple(candidates)
    if "*" in tokens:
        raise ValueError(f"{label} wildcard '*' cannot be combined with explicit values")
    return _parse_integer_tokens(tokens, label)


def _parse_integer_tokens(tokens: list[str], label: str) -> tuple[int, ...]:
    """Convert explicit selection tokens to integers.

    Returns:
        Parsed integer selection.

    Raises:
        ValueError: If a token is not an integer.

    """
    try:
        return tuple(int(token) for token in tokens)
    except ValueError as error:
        raise ValueError(f"{label} must be '*' or comma-separated integers") from error


def _validate_selection_values(
    selected: tuple[int, ...],
    candidates: Sequence[int],
    *,
    label: str,
    minimum: int,
    maximum: int,
) -> None:
    """Reject duplicate, out-of-range, or unavailable selections."""
    _reject_duplicates(selected, label)
    _reject_out_of_range(selected, label, minimum, maximum)
    _reject_unavailable(selected, candidates, label)


def _reject_duplicates(selected: tuple[int, ...], label: str) -> None:
    """Reject duplicate selection values.

    Raises:
        ValueError: If the selection contains duplicates.

    """
    if len(set(selected)) != len(selected):
        raise ValueError(f"{label} selection cannot contain duplicates")


def _reject_out_of_range(
    selected: tuple[int, ...],
    label: str,
    minimum: int,
    maximum: int,
) -> None:
    """Reject selection values outside their allowed domain.

    Raises:
        ValueError: If a value is below the minimum or above the maximum.

    """
    invalid = [item for item in selected if not minimum <= item <= maximum]
    if invalid:
        raise ValueError(f"{label} values must be between {minimum} and {maximum}: {invalid}")


def _reject_unavailable(
    selected: tuple[int, ...],
    candidates: Sequence[int],
    label: str,
) -> None:
    """Reject selection values not present in the complete run.

    Raises:
        RetrievalError: If a selection is absent from the complete run.

    """
    unavailable = [item for item in selected if item not in candidates]
    if unavailable:
        raise RetrievalError(f"{label} are not available in this complete run: {unavailable}")


def _available_selections(manifest: dict[str, object]) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Return sorted member and lead IDs recorded in a complete manifest.

    Returns:
        Available members and forecast leads.

    Raises:
        RetrievalError: If the manifest contains invalid field records.

    """
    fields = manifest["fields"]
    if not isinstance(fields, list):
        raise RetrievalError("Retrieval manifest has no field inventory")
    members: set[int] = set()
    leads: dict[int, None] = {}
    for field in fields:
        member, lead = _manifest_field_ids(field)
        members.add(member)
        leads[lead] = None
    return tuple(sorted(members)), tuple(leads)


def _manifest_field_ids(field: object) -> tuple[int, int]:
    """Validate and return member and lead IDs from a manifest field entry.

    Returns:
        The validated member and lead.

    """
    return _manifest_integer(field, "member"), _manifest_integer(field, "lead_hours")


def _manifest_integer(field: object, key: str) -> int:
    """Read an integer field ID from a manifest entry.

    Returns:
        The integer ID associated with key.

    Raises:
        RetrievalError: If the field or ID is invalid.

    """
    if not isinstance(field, dict):
        raise RetrievalError("Retrieval manifest contains an invalid field record")
    value = field.get(key)
    if not isinstance(value, int) or isinstance(value, bool):
        raise RetrievalError("Retrieval manifest contains invalid member/lead IDs")
    return value


def _write_provenance(context: _QuicklookContext) -> None:
    """Persist JSON provenance and image digest alongside the PNG."""
    minimum = float(np.min(context.values))
    maximum = float(np.max(context.values))
    sidecar = {
        "created_utc": datetime.now(UTC).isoformat(),
        "source_manifest": str((context.run_dir / "manifest.json").resolve()),
        "coverage_id": context.manifest.get("coverage_id"),
        "initialization_utc": context.initialization.isoformat(),
        "member": context.member,
        "lead_hours": context.lead_hours,
        "grib_metadata": context.metadata,
        "field_minimum": minimum,
        "field_maximum": maximum,
        "source_credit": SOURCE_CREDIT,
        "projection": "equirectangular",
        "output_sha256": hashlib.sha256(context.output_path.read_bytes()).hexdigest(),
    }
    sidecar_path = context.output_path.with_suffix(".json")
    temporary_sidecar_path = sidecar_path.with_suffix(".json.tmp")
    temporary_sidecar_path.write_text(
        json.dumps(sidecar, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_sidecar_path.replace(sidecar_path)


def render_quicklooks(
    run_dir: Path,
    *,
    lead_hours: str | Sequence[int],
    members: str | Sequence[int] = "0",
    output_dir: Path | None = None,
    output_path: Path | None = None,
) -> tuple[Path, ...]:
    """Render selected members and leads with one batch progress bar.

    Returns:
        Paths to generated PNG quicklooks in lead-major, member-minor order.

    """
    jobs = _resolve_quicklook_jobs(
        run_dir,
        lead_hours=lead_hours,
        members=members,
        output_dir=output_dir,
        output_path=output_path,
    )
    return _render_quicklook_jobs(
        run_dir,
        jobs,
        output_dir=output_dir,
        output_path=output_path,
    )


def _resolve_quicklook_jobs(
    run_dir: Path,
    *,
    lead_hours: str | Sequence[int],
    members: str | Sequence[int],
    output_dir: Path | None,
    output_path: Path | None,
) -> tuple[tuple[int, int], ...]:
    """Validate selections and output options, then resolve requested fields.

    Returns:
        Validated (lead, member) pairs in output order.

    """
    manifest = _read_manifest(run_dir)
    available_members, available_leads = _available_selections(manifest)
    selected_members = _selected_members(members)
    _check_available_members(selected_members, available_members)
    selected_leads = _selected_leads(lead_hours, available_leads)
    jobs = tuple(product(selected_leads, selected_members))
    _validate_output_selection(jobs, output_dir=output_dir, output_path=output_path)
    _validate_requested_fields(manifest, jobs)
    return jobs


def _selected_members(members: str | Sequence[int]) -> tuple[int, ...]:
    """Parse and validate selected ensemble members.

    Returns:
        Validated member identifiers.

    """
    return _parse_selection(
        members,
        tuple(range(35)),
        label="members",
        minimum=0,
        maximum=34,
    )


def _selected_leads(
    lead_hours: str | Sequence[int],
    available_leads: tuple[int, ...],
) -> tuple[int, ...]:
    """Parse and validate selected forecast leads.

    Returns:
        Validated forecast lead hours.

    """
    return _parse_selection(
        lead_hours,
        available_leads,
        label="lead hours",
        minimum=0,
        maximum=102,
    )


def _validate_output_selection(
    jobs: tuple[tuple[int, int], ...],
    *,
    output_dir: Path | None,
    output_path: Path | None,
) -> None:
    """Validate mutually exclusive output options and single-job constraints.

    Raises:
        ValueError: If output options conflict or --output selects multiple jobs.

    """
    if output_path is not None and len(jobs) != 1:
        raise ValueError("--output can only be used when selecting one member/lead pair")
    if output_dir is not None and output_path is not None:
        raise ValueError("--output and --output-dir cannot be combined")


def _validate_requested_fields(
    manifest: dict[str, object],
    jobs: tuple[tuple[int, int], ...],
) -> None:
    """Ensure every requested lead/member pair is present in the manifest."""
    for lead, member in jobs:
        _find_field(manifest, member=member, lead_hours=lead)


def _check_available_members(
    selected_members: tuple[int, ...],
    available_members: tuple[int, ...],
) -> None:
    """Raise when a selected member is not in the complete run.

    Raises:
        RetrievalError: If a selected member is missing from the complete run.

    """
    unavailable_members = [member for member in selected_members if member not in available_members]
    if unavailable_members:
        raise RetrievalError(
            f"Members are not available in this complete run: {unavailable_members}"
        )


def _render_quicklook_jobs(
    run_dir: Path,
    jobs: tuple[tuple[int, int], ...],
    *,
    output_dir: Path | None,
    output_path: Path | None,
) -> tuple[Path, ...]:
    """Render validated selections and return their output paths.

    Returns:
        Paths to generated PNG quicklooks in lead-major, member-minor order.

    """
    outputs: list[Path] = []
    with tqdm(
        total=len(jobs),
        desc="Quicklooks",
        unit="map",
        dynamic_ncols=True,
    ) as progress:
        for lead, member in jobs:
            progress.set_description(f"Quicklook Z500 | t+{lead}h | member {member:03}")
            selected_output = output_path
            if selected_output is None:
                destination = output_dir or (run_dir / "maps")
                selected_output = destination / f"z500_member_{member:03}_lead_{lead:03}.png"
            context = _load_quicklook_context(
                run_dir,
                lead_hours=lead,
                member=member,
                output_path=selected_output,
            )
            _render_png(context)
            _write_provenance(context)
            outputs.append(context.output_path)
            progress.update()
            logger.info(
                "Rendered variable=Z500 lead=%03dh member=%03d map=%s",
                lead,
                member,
                context.output_path.resolve(),
            )
    return tuple(outputs)


def render_quicklook(
    run_dir: Path,
    *,
    lead_hours: int,
    member: int = 0,
    output_path: Path | None = None,
) -> Path:
    """Render one selected member/lead as an equirectangular PNG quicklook.

    Returns:
        Path to the generated PNG.

    """
    return render_quicklooks(
        run_dir,
        lead_hours=(lead_hours,),
        members=(member,),
        output_path=output_path,
    )[0]
