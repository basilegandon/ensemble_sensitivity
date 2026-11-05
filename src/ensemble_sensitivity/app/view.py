# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Panel/Bokeh view: two linked maps, selectors and a status line (thin glue over the model)."""

from __future__ import annotations

import logging
from importlib import import_module
from typing import TYPE_CHECKING, Any

import cartopy.feature as cfeature
import matplotlib as mpl
import numpy as np
from matplotlib import colors as mpl_colors

from ensemble_sensitivity.app.model import (
    RADIUS_MAX_KM,
    RADIUS_MIN_KM,
    AppModel,
    AppSelectionError,
    MapsData,
    contour_segments,
)
from ensemble_sensitivity.sensitivity.render import SOURCE_CREDIT
from ensemble_sensitivity.sensitivity.statistics import StatisticsError
from ensemble_sensitivity.sensitivity.zone import ZoneError

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

# Panel and Bokeh are loaded dynamically: their dynamic APIs do not pass strict type checking.
pn: Any = import_module("panel")
bokeh_models: Any = import_module("bokeh.models")
bokeh_palettes: Any = import_module("bokeh.palettes")
bokeh_plotting: Any = import_module("bokeh.plotting")

LOGGER = logging.getLogger(__name__)
MAP_WIDTH = 640
MAP_HEIGHT = 340
EXPECTED_ERRORS = (AppSelectionError, StatisticsError, ZoneError)
RADIUS_STEP_KM = 50.0
EMPTY_IMAGE = np.full((2, 2), np.nan, dtype=np.float32)


def diverging_palette() -> list[str]:
    """Sample matplotlib's RdBu_r (as in the PNG maps) into 256 hex colours.

    Returns:
        Colours from -1 (blue) to +1 (red).

    """
    colormap = mpl.colormaps["RdBu_r"]
    return [mpl_colors.to_hex(colormap(step / 255)) for step in range(256)]


def coastline_segments() -> tuple[list[list[float]], list[list[float]]]:
    """Read the 110 m coastlines; an unavailable shapefile only drops the overlay.

    Returns:
        Longitude and latitude lists for a multi-line glyph (empty if unavailable).

    """
    xs: list[list[float]] = []
    ys: list[list[float]] = []
    try:
        geometries = list(cfeature.COASTLINE.with_scale("110m").geometries())
    except OSError:
        LOGGER.warning("Coastline data unavailable: the maps are drawn without coastlines.")
        return xs, ys
    for geometry in geometries:
        for part in getattr(geometry, "geoms", [geometry]):
            longitudes, latitudes = part.xy
            xs.append(list(longitudes))
            ys.append(list(latitudes))
    return xs, ys


def _map_figure(title: str, x_range: Any, y_range: Any) -> Any:
    plot = bokeh_plotting.figure(
        title=f"{title} | {SOURCE_CREDIT}",
        width=MAP_WIDTH,
        height=MAP_HEIGHT,
        x_range=x_range,
        y_range=y_range,
        tools="pan,wheel_zoom,box_zoom,reset,save,tap",
        active_scroll="wheel_zoom",
        x_axis_label="Longitude (°E)",
        y_axis_label="Latitude (°N)",
    )
    plot.toolbar.logo = None
    return plot


class AppView:
    """Builds the layout and routes widget and tap events to the model."""

    def __init__(self, model: AppModel) -> None:
        """Create widgets, figures and data sources, then draw the first selection."""
        self.model = model
        self._syncing = False
        self._build_sources()
        self._build_figures()
        self._build_widgets()
        self.layout = self._build_layout()
        self.refresh()

    def _build_sources(self) -> None:
        self.correlation_source = bokeh_models.ColumnDataSource({"image": [EMPTY_IMAGE]})
        self.std_source = bokeh_models.ColumnDataSource({"image": [EMPTY_IMAGE]})
        self.contour_source = bokeh_models.ColumnDataSource({"xs": [], "ys": []})
        self.zone_source = bokeh_models.ColumnDataSource({"x": [], "y": []})
        self.zone_source_left = bokeh_models.ColumnDataSource({"x": [], "y": []})
        xs, ys = coastline_segments()
        self.coast_source = bokeh_models.ColumnDataSource({"xs": xs, "ys": ys})

    def _build_figures(self) -> None:
        x_range, y_range = bokeh_models.Range1d(-180.0, 180.0), bokeh_models.Range1d(-90.0, 90.0)
        self.correlation_mapper = bokeh_models.LinearColorMapper(
            palette=diverging_palette(), low=-1.0, high=1.0, nan_color=(0, 0, 0, 0)
        )
        self.std_mapper = bokeh_models.LinearColorMapper(
            palette=bokeh_palettes.Viridis256, low=0.0, high=1.0
        )
        self.left = _map_figure("Corrélation de Pearson (signée)", x_range, y_range)
        self.right = _map_figure("Moyenne (isolignes) et écart-type (fond)", x_range, y_range)
        self._images = (
            self._draw_image(self.left, self.correlation_source, self.correlation_mapper),
            self._draw_image(self.right, self.std_source, self.std_mapper),
        )
        for plot, zone in ((self.left, self.zone_source_left), (self.right, self.zone_source)):
            plot.multi_line("xs", "ys", source=self.coast_source, line_color="#333333", alpha=0.7)
            plot.line("x", "y", source=zone, line_color="black", line_width=2)
            plot.on_event("tap", self.on_tap)
        self.right.multi_line(
            "xs", "ys", source=self.contour_source, line_color="white", line_width=1.2
        )
        self.left.add_layout(
            bokeh_models.ColorBar(
                color_mapper=self.correlation_mapper, ticker=bokeh_models.BasicTicker()
            ),
            "right",
        )
        self.right.add_layout(
            bokeh_models.ColorBar(
                color_mapper=self.std_mapper,
                ticker=bokeh_models.BasicTicker(),
                title="écart-type (m² s⁻²)",
            ),
            "right",
        )

    @staticmethod
    def _draw_image(plot: Any, source: Any, mapper: Any) -> Any:
        return plot.image(
            image="image", x=-180, y=-90, dw=360, dh=180, source=source, color_mapper=mapper
        )

    def _build_widgets(self) -> None:
        model = self.model
        self.run_select = pn.widgets.Select(label="Run", options=model.run_labels())
        self.target_select = pn.widgets.Select(label="Échéance cible (h)")
        self.predictor_select = pn.widgets.Select(label="Échéance prédicteur (h)")
        self.latitude_input = pn.widgets.FloatInput(label="Latitude (°N)", step=0.5)
        self.longitude_input = pn.widgets.FloatInput(label="Longitude (°E)", step=0.5)
        self.radius_slider = pn.widgets.FloatSlider(
            label="Rayon (km)", start=RADIUS_MIN_KM, end=RADIUS_MAX_KM, step=RADIUS_STEP_KM
        )
        self.status = pn.pane.Markdown("")
        self._sync_widgets(reset_run=True)
        self.run_select.param.watch(self._on_run, "value")
        self.target_select.param.watch(self._on_target, "value")
        self.predictor_select.param.watch(self._on_predictor, "value")
        self.latitude_input.param.watch(self._on_centre, "value")
        self.longitude_input.param.watch(self._on_centre, "value")
        self.radius_slider.param.watch(self._on_radius, "value_throttled")

    def _build_layout(self) -> Any:
        controls = pn.Row(
            self.run_select,
            self.target_select,
            self.predictor_select,
            self.latitude_input,
            self.longitude_input,
            self.radius_slider,
        )
        maps = pn.Row(pn.pane.Bokeh(self.left), pn.pane.Bokeh(self.right))
        footer = pn.pane.Markdown(
            f"{SOURCE_CREDIT} - Clic sur une carte : centre de la zone cible (moyenne pondérée "
            "par l'aire, cos φ)."
        )
        return pn.Column(controls, self.status, maps, footer)

    def _sync_widgets(self, *, reset_run: bool = False) -> None:
        selection = self.model.selection
        self._syncing = True
        try:
            if reset_run:
                self.run_select.value = self.model.run_id
            self.target_select.options = list(self.model.leads[1:])
            self.target_select.value = selection.target_lead_hours
            self.predictor_select.options = list(
                self.model.predictor_options(selection.target_lead_hours)
            )
            self.predictor_select.value = selection.predictor_lead_hours
            self.latitude_input.value = selection.center_latitude
            self.longitude_input.value = selection.center_longitude
            self.radius_slider.value = selection.radius_km
        finally:
            self._syncing = False

    def _apply(self, change: Callable[[], object]) -> None:
        if self._syncing:
            return
        refused: Exception | None = None
        try:
            change()
        except EXPECTED_ERRORS as error:
            refused = error
        self._sync_widgets()
        self.refresh()
        if refused is not None:
            self._show_error(refused)

    def _on_run(self, event: Any) -> None:
        self._apply(lambda: self.model.select_run(event.new))

    def _on_target(self, event: Any) -> None:
        self._apply(lambda: self.model.update(target_lead_hours=event.new))

    def _on_predictor(self, event: Any) -> None:
        self._apply(lambda: self.model.update(predictor_lead_hours=event.new))

    def _on_centre(self, _event: Any) -> None:
        self._apply(
            lambda: self.model.update(
                center_latitude=self.latitude_input.value,
                center_longitude=self.longitude_input.value,
            )
        )

    def _on_radius(self, event: Any) -> None:
        self._apply(lambda: self.model.update(radius_km=event.new))

    def on_tap(self, event: Any) -> None:
        """Move the zone centre to the clicked point."""
        self._apply(lambda: self.model.update(center_latitude=event.y, center_longitude=event.x))

    def _show_error(self, error: Exception) -> None:
        LOGGER.warning("Selection refused: %s", error)
        self.status.object = f"**Erreur :** {error}"

    def refresh(self) -> None:
        """Recompute both maps for the current selection and push them to the browser."""
        try:
            data = self.model.maps()
        except EXPECTED_ERRORS as error:
            self._show_error(error)
            return
        self._push(data)

    def _push(self, data: MapsData) -> None:
        model = self.model
        longitudes, latitudes = model.display_longitudes, model.display_latitudes
        step_x = float(longitudes[1] - longitudes[0])
        step_y = float(latitudes[1] - latitudes[0])
        extent = {
            "x": float(longitudes[0]) - step_x / 2,
            "y": float(latitudes[0]) - step_y / 2,
        }
        for image in self._images:
            image.glyph.update(
                x=extent["x"],
                y=extent["y"],
                dw=step_x * len(longitudes),
                dh=step_y * len(latitudes),
            )
        self.correlation_source.data = {"image": [data.correlation]}
        self.std_source.data = {"image": [data.std]}
        self.std_mapper.update(low=float(np.nanmin(data.std)), high=float(np.nanmax(data.std)))
        xs, ys = contour_segments(data.mean, longitudes, latitudes, data.levels)
        self.contour_source.data = {"xs": xs, "ys": ys}
        zone = {"x": data.outline_longitudes.tolist(), "y": data.outline_latitudes.tolist()}
        self.zone_source.data = zone
        self.zone_source_left.data = dict(zone)
        selection = data.selection
        contours = (
            f"contours {data.levels[0]:g} à {data.levels[-1]:g} m² s⁻² (géopotentiel)"
            if data.levels
            else "sans contour"
        )
        self.status.object = (
            f"Run `{model.run_id[:8]}` | init {model.initialization:%Y-%m-%d %H} UTC | "
            f"cible t+{selection.target_lead_hours} h, prédicteur "
            f"t+{selection.predictor_lead_hours} h | {data.zone_cell_count} mailles | "
            f"{data.constant_points} points constants (NaN) | {contours}"
        )


def build_view(output_root: Path, run_id: str | None = None) -> AppView:
    """Open the run and build the view.

    Returns:
        The view; its ``layout`` attribute is the Panel layout to serve.

    """
    return AppView(AppModel(output_root, run_id))


def serve(output_root: Path, run_id: str | None = None, *, port: int, show: bool) -> None:
    """Serve the application on the loopback interface until interrupted.

    The run is opened first so that a missing or incomplete run fails before the server starts.
    """
    app = build_view(output_root, run_id)
    LOGGER.info("Serving the sensitivity application on http://127.0.0.1:%d", port)
    pn.serve(app.layout, address="127.0.0.1", port=port, show=show, title="Ensemble sensitivity")
