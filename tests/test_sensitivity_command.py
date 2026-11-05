# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Synthetic tests for the sensitivity command, rendering metadata and CLI wiring."""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import replace
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


def _make_run(root: Path, *, created_day: int = 1, leads: tuple[int, ...] = ()) -> str:
    run_id = new_run_id()
    raw = root / run_id / "raw_fetch"
    raw.mkdir(parents=True)
    (raw / "manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "fields": [{"member": 0, "lead_hours": lead} for lead in leads],
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


@pytest.mark.parametrize("pole", [90.0, -90.0])
def test_zone_outline_traces_the_polar_circle(pole: float) -> None:
    lon, lat = render.zone_outline(pole, 0.0, 500.0)
    assert lat == pytest.approx(pole - math.copysign(math.degrees(500.0 / EARTH_RADIUS_KM), pole))
    wrapped = np.mod(lon, 360.0)
    assert np.ptp(wrapped) > 300.0
    assert len(np.unique(np.round(wrapped, 3))) > 10


def test_output_stem_distinguishes_close_requests(tmp_path: Path) -> None:
    first = replace(_request(tmp_path, None), center_latitude=45.501)
    second = replace(first, center_latitude=45.504)
    assert command.output_stem(first, 24, 0) != command.output_stem(second, 24, 0)
    longer = replace(first, radius_km=300.0000001)
    assert command.output_stem(first, 24, 0) != command.output_stem(longer, 24, 0)


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


def _request(
    root: Path,
    run_id: str | None,
    *,
    target: int = 24,
    predictor: int | None = 0,
) -> command.SensitivityRequest:
    return command.SensitivityRequest(
        output_root=root,
        run_id=run_id,
        target_lead_hours=target,
        predictor_lead_hours=predictor,
        center_latitude=45.5,
        center_longitude=-3.0,
        radius_km=300.0,
        show_progress=False,
    )


def test_plan_pairs_builds_the_triangle_without_trivial_pairs() -> None:
    leads = (0, 3, 6, 9)
    assert command.plan_pairs(leads, 9, None) == ((9, (0, 3, 6)), (6, (0, 3)), (3, (0,)))
    assert command.plan_pairs(leads, 6, None) == ((6, (0, 3)), (3, (0,)))
    assert command.plan_pairs(leads, 9, 3) == ((9, (3,)),)


@pytest.mark.parametrize(
    ("target", "predictor", "message"),
    [
        (7, None, "Target lead 7 h"),
        (0, None, "no earlier available lead"),
        (6, 6, "Predictor lead 6 h"),
        (6, 4, "Predictor lead 4 h"),
        (3, 9, "Predictor lead 9 h"),
    ],
)
def test_plan_pairs_rejects_unusable_leads(
    target: int, predictor: int | None, message: str
) -> None:
    with pytest.raises(command.SensitivityCommandError, match=message):
        command.plan_pairs((0, 3, 6, 9), target, predictor)


def _small_grid_run(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    leads: tuple[int, ...],
) -> tuple[str, list[tuple[int, int]]]:
    rng = np.random.default_rng(3)
    base = rng.normal(5500.0, 30.0, size=(35, 11, 10))
    calls: list[tuple[int, int]] = []

    def loader(*_args: object) -> object:
        def load(member: int, lead: int) -> np.ndarray:
            calls.append((member, lead))
            return base[member] + lead * (1.0 + member / 35.0)

        return load

    monkeypatch.setattr(command, "run_field_loader", loader)
    monkeypatch.setattr(
        command,
        "grid_axes",
        lambda: (np.linspace(50.0, 40.0, 11), np.arange(350.0, 360.0)),
    )
    return _make_run(root, leads=leads), calls


def test_run_sensitivity_computes_every_pair_with_one_target_pass_per_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, calls = _small_grid_run(tmp_path, monkeypatch, (0, 3, 6))
    rendered: list[render.SensitivityFigure] = []
    monkeypatch.setattr(command, "render_sensitivity", lambda data, _path: rendered.append(data))

    outputs = command.run_sensitivity(_request(tmp_path, run_id, target=6, predictor=None))

    maps = tmp_path / run_id / "maps"
    assert outputs == (
        maps / "sensitivity_t006_from_t000_lat+45.5_lon-3.0_r300.0km.png",
        maps / "sensitivity_t006_from_t003_lat+45.5_lon-3.0_r300.0km.png",
        maps / "sensitivity_t003_from_t000_lat+45.5_lon-3.0_r300.0km.png",
    )
    assert [(f.result.target_lead_hours, f.result.predictor_lead_hours) for f in rendered] == [
        (6, 0),
        (6, 3),
        (3, 0),
    ]
    assert all(f.run_id == run_id and f.initialization == INITIALIZATION for f in rendered)
    # 2 target passes + 3 predictor passes, 35 members each: one field at a time.
    assert len(calls) == 5 * 35
    assert [lead for _member, lead in calls[: 35 * 3]] == [6] * 35 + [0] * 35 + [3] * 35


def test_run_sensitivity_single_pair_and_unknown_lead(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id, _calls = _small_grid_run(tmp_path, monkeypatch, (0, 3, 6))
    monkeypatch.setattr(command, "render_sensitivity", lambda _data, _path: None)

    single = command.run_sensitivity(_request(tmp_path, run_id, target=6, predictor=3))
    assert [path.name[:26] for path in single] == ["sensitivity_t006_from_t003"]
    request = _request(tmp_path, run_id, target=4, predictor=None)
    with pytest.raises(command.SensitivityCommandError, match="Target lead 4 h"):
        command.run_sensitivity(request)


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
    main_module.main([*arguments[:5], *arguments[7:]])
    assert calls[1].predictor_lead_hours is None
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
