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

from ensemble_sensitivity.wcs_retrieval import (
    MAX_RESPONSE_BYTES,
    RetrievalError,
    _Eccodes,
    _load_eccodes,
    _validate_grib,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

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
        if (
            isinstance(value, dict)
            and value.get("member") == member
            and value.get("lead_hours") == lead_hours
        ):
            return cast("dict[str, object]", value)
    raise RetrievalError(
        f"Complete run manifest has no validated member {member:03}, lead {lead_hours} h"
    )


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
        ni = eccodes.codes_get(handle, "Ni")
        nj = eccodes.codes_get(handle, "Nj")
        if not isinstance(ni, int) or not isinstance(nj, int) or (ni, nj) != (1440, 721):
            raise RetrievalError(f"Unexpected Z500 grid dimensions: {ni} x {nj}")

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
        longitude_axis = longitudes[0, :]
        latitude_axis = latitudes[:, 0]
        if not np.allclose(longitudes, longitude_axis[None, :], atol=1e-5, rtol=0):
            raise RetrievalError("Z500 longitude coordinates are not a regular lat/lon grid")
        if not np.allclose(latitudes, latitude_axis[:, None], atol=1e-5, rtol=0):
            raise RetrievalError("Z500 latitude coordinates are not a regular lat/lon grid")
        if (
            not np.allclose(longitude_axis[[0, -1]], [0.0, 359.75], atol=1e-5, rtol=0)
            or not np.allclose(latitude_axis[[0, -1]], [90.0, -90.0], atol=1e-5, rtol=0)
            or not np.allclose(
                np.diff(longitude_axis),
                0.25,
                atol=1e-5,
                rtol=0,
            )
            or not np.allclose(np.diff(latitude_axis), -0.25, atol=1e-5, rtol=0)
        ):
            raise RetrievalError("Z500 field does not use the documented global 0.25-degree grid")
        values = np.roll(values, shift=-(ni // 2), axis=1)
        longitude_axis = np.concatenate(
            (longitude_axis[ni // 2 :] - 360.0, longitude_axis[: ni // 2])
        )
        return values, longitude_axis, latitude_axis
    finally:
        eccodes.codes_release(handle)


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

    Raises:
        RetrievalError: If a field, manifest, or ecCodes metadata is invalid.
        ValueError: If a requested output path is not a PNG.

    """
    manifest = _read_manifest(run_dir)
    field_record = _find_field(manifest, member=member, lead_hours=lead_hours)
    relative_path = field_record.get("relative_path")
    if not isinstance(relative_path, str) or Path(relative_path).name != relative_path:
        raise RetrievalError("Manifest field path must be a local filename")
    field_path = run_dir / relative_path
    if field_path.resolve().parent != run_dir.resolve():
        raise RetrievalError(f"Manifest field path escapes its run directory: {relative_path}")
    body = field_path.read_bytes()
    if len(body) > MAX_RESPONSE_BYTES:
        raise RetrievalError(f"Selected field exceeds {MAX_RESPONSE_BYTES} bytes: {field_path}")

    initialization_value = manifest.get("initialization_utc")
    if not isinstance(initialization_value, str):
        raise RetrievalError("Retrieval manifest has no initialization timestamp")
    try:
        initialization = datetime.fromisoformat(initialization_value)
    except ValueError as error:
        message = "Retrieval manifest has an invalid initialization timestamp"
        raise RetrievalError(message) from error
    if initialization.tzinfo is None:
        raise RetrievalError("Retrieval initialization timestamp must include a timezone")
    eccodes = _load_eccodes()
    metadata = _validate_grib(
        body,
        eccodes,
        member=member,
        lead_hours=lead_hours,
        initialization=initialization.astimezone(UTC),
    )
    values, longitude_axis, latitude_axis = _decode_arrays(body, cast("_ArrayEccodes", eccodes))
    if output_path is None:
        output_path = run_dir / f"z500_member_{member:03}_lead_{lead_hours:03}.png"
    if output_path.suffix.casefold() != ".png":
        raise ValueError("Quicklook output path must use the .png suffix")
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


def _render_png(context: _QuicklookContext) -> None:
    """Save a global equirectangular PNG for the selected field."""
    output_path = context.output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = output_path.with_name(f"{output_path.stem}.tmp.png")
    geographic_crs = ccrs.PlateCarree()
    figure = plt.figure(figsize=(13, 6.5), constrained_layout=True)
    try:
        axes = figure.add_subplot(1, 1, 1, projection=ccrs.PlateCarree())
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
        RetrievalError: If a requested value is unavailable.
        ValueError: If the selection is malformed, empty, duplicated, or out of range.

    """
    if isinstance(value, str):
        tokens = [token.strip() for token in value.split(",")]
        if tokens == ["*"]:
            selected = tuple(candidates)
        elif "*" in tokens:
            raise ValueError(f"{label} wildcard '*' cannot be combined with explicit values")
        else:
            try:
                selected = tuple(int(token) for token in tokens)
            except ValueError as error:
                raise ValueError(f"{label} must be '*' or comma-separated integers") from error
    else:
        selected = tuple(value)
    if not selected:
        raise ValueError(f"{label} selection cannot be empty")
    if len(set(selected)) != len(selected):
        raise ValueError(f"{label} selection cannot contain duplicates")
    invalid = [item for item in selected if not minimum <= item <= maximum]
    if invalid:
        raise ValueError(f"{label} values must be between {minimum} and {maximum}: {invalid}")
    unavailable = [item for item in selected if item not in candidates]
    if unavailable:
        raise RetrievalError(f"{label} are not available in this complete run: {unavailable}")
    return selected


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
        if not isinstance(field, dict):
            raise RetrievalError("Retrieval manifest contains an invalid field record")
        member = field.get("member")
        lead = field.get("lead_hours")
        if (
            not isinstance(member, int)
            or isinstance(member, bool)
            or not isinstance(lead, int)
            or isinstance(lead, bool)
        ):
            raise RetrievalError("Retrieval manifest contains invalid member/lead IDs")
        members.add(member)
        leads[lead] = None
    return tuple(sorted(members)), tuple(leads)


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

    Raises:
        RetrievalError: If selected fields are missing from the complete run.
        ValueError: If a selection or output path is invalid.

    """
    manifest = _read_manifest(run_dir)
    available_members, available_leads = _available_selections(manifest)
    selected_members = _parse_selection(
        members,
        tuple(range(35)),
        label="members",
        minimum=0,
        maximum=34,
    )
    selected_leads = _parse_selection(
        lead_hours,
        available_leads,
        label="lead hours",
        minimum=0,
        maximum=102,
    )
    unavailable_members = [member for member in selected_members if member not in available_members]
    if unavailable_members:
        raise RetrievalError(
            f"Members are not available in this complete run: {unavailable_members}"
        )
    jobs = tuple(product(selected_leads, selected_members))
    if output_path is not None and len(jobs) != 1:
        raise ValueError("--output can only be used when selecting one member/lead pair")
    if output_dir is not None and output_path is not None:
        raise ValueError("--output and --output-dir cannot be combined")
    for lead, member in jobs:
        _find_field(manifest, member=member, lead_hours=lead)

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
