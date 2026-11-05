# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Headless tests of the Panel/Bokeh view: widget and tap wiring, data sources (no browser)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING

import numpy as np
import pytest

from ensemble_sensitivity import main as main_module
from ensemble_sensitivity.app import view
from tests.app_support import LEADS, make_run, patch_synthetic_grid

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture
def _patched(monkeypatch: pytest.MonkeyPatch) -> None:
    patch_synthetic_grid(monkeypatch)
    monkeypatch.setattr(
        view, "coastline_segments", lambda: ([[-10.0, 0.0, 10.0]], [[40.0, 45.0, 50.0]])
    )


def _open(root: Path) -> view.AppView:
    make_run(root)
    return view.build_view(root)


@pytest.mark.usefixtures("_patched")
def test_first_draw_fills_every_source_and_shows_the_credit(tmp_path: Path) -> None:
    app = _open(tmp_path)
    assert app.correlation_source.data["image"][0].shape == (11, 10)
    assert app.std_source.data["image"][0].shape == (11, 10)
    assert len(app.contour_source.data["xs"]) > 0
    assert len(app.zone_source.data["x"]) == len(app.zone_source_left.data["x"]) > 0
    assert "Météo-France" in app.left.title.text
    assert "Météo-France" in app.right.title.text
    assert "Run" in app.status.object
    assert app.target_select.options == list(LEADS[1:])
    assert app.left.x_range is app.right.x_range
    assert app.left.y_range is app.right.y_range


@pytest.mark.usefixtures("_patched")
def test_target_change_resyncs_predictor_options(tmp_path: Path) -> None:
    app = _open(tmp_path)
    app.target_select.value = 3
    assert app.model.selection.target_lead_hours == 3
    assert app.predictor_select.options == [0]
    assert app.predictor_select.value == 0


@pytest.mark.usefixtures("_patched")
def test_tap_moves_the_zone_centre_and_redraws(tmp_path: Path) -> None:
    app = _open(tmp_path)
    before = list(app.zone_source.data["x"])
    app.on_tap(SimpleNamespace(x=-5.0, y=48.0))
    assert app.model.selection.center_latitude == pytest.approx(48.0)
    assert app.latitude_input.value == pytest.approx(48.0)
    assert app.longitude_input.value == pytest.approx(-5.0)
    assert list(app.zone_source.data["x"]) != before


@pytest.mark.usefixtures("_patched")
def test_out_of_bounds_request_is_refused_and_widgets_are_restored(tmp_path: Path) -> None:
    app = _open(tmp_path)
    previous = app.model.selection
    app.latitude_input.value = 120.0
    assert "Erreur" in app.status.object
    assert app.model.selection == previous
    assert app.latitude_input.value == previous.center_latitude


@pytest.mark.usefixtures("_patched")
def test_radius_change_updates_the_selection(tmp_path: Path) -> None:
    app = _open(tmp_path)
    app.radius_slider.value_throttled = 500.0
    assert app.model.selection.radius_km == pytest.approx(500.0)
    assert np.isfinite(app.correlation_source.data["image"][0]).any()


def test_coastline_segments_tolerates_missing_data(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*_args: object) -> object:
        raise OSError

    monkeypatch.undo()
    monkeypatch.setattr(view.cfeature.NaturalEarthFeature, "with_scale", broken)
    assert view.coastline_segments() == ([], [])


def test_cli_app_passes_options_to_serve_and_reports_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[tuple[Path, str | None, int, bool]] = []

    def fake_serve(root: Path, run_id: str | None = None, *, port: int, show: bool) -> None:
        calls.append((root, run_id, port, show))

    monkeypatch.setattr(view, "serve", fake_serve)
    main_module.main(["app", "--output-dir", str(tmp_path), "--port", "5100", "--no-browser"])
    main_module.main(["app", "--output-dir", str(tmp_path), "--run-id", "abc"])
    assert calls == [(tmp_path, None, 5100, False), (tmp_path, "abc", 5006, True)]

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise ValueError

    monkeypatch.setattr(view, "serve", refuse)
    with pytest.raises(SystemExit) as excinfo:
        main_module.main(["app", "--output-dir", str(tmp_path)])
    assert excinfo.value.code == 1


def test_serve_binds_to_loopback_after_opening_the_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    patch_synthetic_grid(monkeypatch)
    monkeypatch.setattr(view, "coastline_segments", lambda: ([], []))
    make_run(tmp_path)
    served: list[dict[str, object]] = []
    monkeypatch.setattr(view.pn, "serve", lambda _layout, **options: served.append(options))
    view.serve(tmp_path, port=5111, show=False)
    assert served[0]["address"] == "127.0.0.1"
    assert served[0]["port"] == 5111
    with pytest.raises(ValueError, match="run"):
        view.serve(tmp_path / "missing", port=5111, show=False)
