# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic tests for the sensitivity command, rendering metadata and CLI wiring."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np
import pytest
from cartopy.mpl.geoaxes import GeoAxes

from ensemble_sensitivity import main as main_module
from ensemble_sensitivity.run_index import new_run_id, register_run
from ensemble_sensitivity.sensitivity import command, render
from ensemble_sensitivity.sensitivity.compute import SensitivityResult
from ensemble_sensitivity.sensitivity.zone import EARTH_RADIUS_KM

if TYPE_CHECKING:
    from pathlib import Path

INITIALIZATION = datetime(2026, 1, 1, 0, tzinfo=UTC)


def _result(correlation: np.ndarray | None = None) -> SensitivityResult:
    values = np.linspace(-1.0, 1.0, 12).reshape(3, 4) if correlation is None else correlation
    return SensitivityResult(
        correlation=values,
        target_values=np.array([1.0, 2.0, 4.0]),
        constant_points=1,
        zone_cell_count=7,
        member_count=3,
        target_lead_hours=24,
        predictor_lead_hours=0,
    )


def _figure(result: SensitivityResult) -> render.SensitivityFigure:
    return render.SensitivityFigure(
        result=result,
        longitudes=np.array([0.0, 90.0, 180.0, 270.0]),
        latitudes=np.array([45.0, 0.0, -45.0]),
        run_id="abc",
        coverage_id="cov",
        initialization=INITIALIZATION,
        center_latitude=10.0,
        center_longitude=20.0,
        radius_km=500.0,
    )


def _make_run(root: Path, *, created_day: int = 1) -> str:
    run_id = new_run_id()
    raw = root / run_id / "raw_fetch"
    raw.mkdir(parents=True)
    (raw / "manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "fields": [],
                "coverage_id": "cov",
                "initialization_utc": INITIALIZATION.isoformat(),
                "run_id": run_id,
            }
        ),
        encoding="utf-8",
    )
    register_run(root, run_id, {"coverage_id": "cov"}, datetime(2026, 1, created_day, tzinfo=UTC))
    return run_id


def test_zone_outline_points_lie_at_the_requested_distance() -> None:
    lon, lat = render.zone_outline(48.0, 2.0, 800.0)
    for lo, la in zip(lon, lat, strict=True):
        p1, p2 = math.radians(48.0), math.radians(float(la))
        cosine = math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(
            math.radians(float(lo) - 2.0)
        )
        assert EARTH_RADIUS_KM * math.acos(min(1.0, cosine)) == pytest.approx(800.0, abs=1e-6)
    assert lon[0] == pytest.approx(lon[-1])


def test_render_writes_png_and_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        render,
        "_draw_png",
        lambda _data, path: path.write_bytes(b"png"),
    )
    output = tmp_path / "maps" / "map.png"
    correlation = np.linspace(-1.0, 1.0, 12).reshape(3, 4)
    correlation[0, 0] = np.nan
    render.render_sensitivity(_figure(_result(correlation)), output)

    sidecar = json.loads(output.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["run_id"] == "abc"
    assert sidecar["source_credit"] == "Source : Météo-France"
    assert sidecar["output_sha256"] == hashlib.sha256(b"png").hexdigest()
    assert sidecar["correlation_minimum"] == pytest.approx(-9 / 11)
    assert sidecar["constant_points"] == 1
    assert (sidecar["target_lead_hours"], sidecar["predictor_lead_hours"]) == (24, 0)


def test_render_rejects_bad_suffix_and_undefined_map(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match=r"\.png"):
        render.render_sensitivity(_figure(_result()), tmp_path / "map.jpg")
    with pytest.raises(ValueError, match="no defined value"):
        render.render_sensitivity(_figure(_result(np.full((3, 4), np.nan))), tmp_path / "m.png")


def test_draw_png_runs_with_stubbed_basemap(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("add_feature", "coastlines"):
        monkeypatch.setattr(GeoAxes, name, lambda *_a, **_k: None, raising=False)
    output = tmp_path / "map.png"
    render.render_sensitivity(_figure(_result()), output)
    assert output.read_bytes().startswith(b"\x89PNG")
    assert not (tmp_path / "map.tmp.png").exists()


def test_resolve_run_uses_index_and_picks_latest(tmp_path: Path) -> None:
    older = _make_run(tmp_path, created_day=1)
    newer = _make_run(tmp_path, created_day=2)
    assert command.resolve_run(tmp_path, older) == (older, tmp_path / older)
    assert command.resolve_run(tmp_path, None)[0] == newer


def test_resolve_run_errors_are_explicit(tmp_path: Path) -> None:
    with pytest.raises(command.SensitivityCommandError, match="run fetch first"):
        command.resolve_run(tmp_path, None)
    known = _make_run(tmp_path)
    with pytest.raises(command.SensitivityCommandError, match="Unknown run"):
        command.resolve_run(tmp_path, "nope")
    (tmp_path / known / "raw_fetch" / "manifest.json").unlink()
    (tmp_path / known).rename(tmp_path / "moved")
    with pytest.raises(command.SensitivityCommandError, match="run fetch first"):
        command.resolve_run(tmp_path, known)


def _request(root: Path, run_id: str | None) -> command.SensitivityRequest:
    return command.SensitivityRequest(
        output_root=root,
        run_id=run_id,
        target_lead_hours=24,
        predictor_lead_hours=0,
        center_latitude=45.5,
        center_longitude=-3.0,
        radius_km=300.0,
    )


def test_run_sensitivity_wires_run_compute_and_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = _make_run(tmp_path)
    seen: dict[str, object] = {}

    def fake_compute(raw_dir: Path, **kwargs: object) -> SensitivityResult:
        seen["raw_dir"] = raw_dir
        seen.update(kwargs)
        return _result()

    def fake_render(data: render.SensitivityFigure, path: Path) -> None:
        seen["figure"] = data
        seen["path"] = path

    monkeypatch.setattr(command, "sensitivity_from_run", fake_compute)
    monkeypatch.setattr(command, "render_sensitivity", fake_render)

    output = command.run_sensitivity(_request(tmp_path, run_id))

    assert seen["raw_dir"] == tmp_path / run_id / "raw_fetch"
    assert seen["members"] == tuple(range(35))
    assert output == tmp_path / run_id / "maps" / (
        "sensitivity_t024_from_t000_lat+45.50_lon-3.00_r300km.png"
    )
    figure = seen["figure"]
    assert isinstance(figure, render.SensitivityFigure)
    assert figure.run_id == run_id
    assert figure.initialization == INITIALIZATION


def test_cli_sensitivity_success_and_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[command.SensitivityRequest] = []
    monkeypatch.setattr(main_module, "run_sensitivity", calls.append)
    arguments = [
        "sensitivity",
        "--output-dir",
        str(tmp_path),
        "--target-lead",
        "48",
        "--predictor-lead",
        "24",
        "--lat",
        "10",
        "--lon",
        "20",
        "--radius-km",
        "250",
    ]
    main_module.main(arguments)
    assert calls[0].target_lead_hours == 48
    assert calls[0].run_id is None
    assert calls[0].radius_km == pytest.approx(250.0)

    with pytest.raises(SystemExit) as excinfo:
        main_module.main(["sensitivity", "--output-dir", str(tmp_path)])
    assert excinfo.value.code == 2

    def boom(_request: command.SensitivityRequest) -> None:
        message = "unknown"
        raise command.SensitivityCommandError(message)

    monkeypatch.setattr(main_module, "run_sensitivity", boom)
    with pytest.raises(SystemExit) as excinfo:
        main_module.main(arguments)
    assert excinfo.value.code == 1
