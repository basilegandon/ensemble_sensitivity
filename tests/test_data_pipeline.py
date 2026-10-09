# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the PEARP retrieval and independent quicklook stages."""

from __future__ import annotations

import io
import json
import time
from datetime import UTC, datetime, timedelta
from email.message import Message
from itertools import pairwise
from typing import TYPE_CHECKING, ClassVar, Self, cast, override
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import cartopy.crs as ccrs
import numpy as np
import pytest
from cartopy.mpl.geoaxes import GeoAxes

from ensemble_sensitivity import visualization, wcs_retrieval
from ensemble_sensitivity.wcs_retrieval import RetrievalError, RetrievalOptions

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType
    from urllib.request import Request


class FakeEccodes:
    """Provide deterministic metadata and optional coordinate arrays."""

    def __init__(self, metadata: dict[str, int | float | str]) -> None:
        """Store deterministic metadata used by the fake binding."""
        self.metadata = metadata
        self.released = False

    def codes_get(self, _handle: object, key: str) -> int | float | str:
        """Return the configured scalar key."""
        return self.metadata[key]

    @staticmethod
    def codes_new_from_message(_message: bytes) -> object:
        """Return a stand-in handle."""
        return object()

    def codes_release(self, _handle: object) -> None:
        """Record handle release."""
        self.released = True

    @staticmethod
    def codes_get_array(_handle: object, key: str) -> object:
        """Return deterministic global-grid fields and coordinate axes.

        Raises:
            KeyError: If the requested array is not part of the fake GRIB message.

        """
        if key == "values":
            return np.full(1440 * 721, 54_000, dtype=np.float32)
        if key == "longitudes":
            return np.tile(np.arange(1440, dtype=np.float64) * 0.25, 721)
        if key == "latitudes":
            return np.repeat(90.0 - np.arange(721, dtype=np.float64) * 0.25, 1440)
        raise KeyError(key)


class FakeResponse:
    """Small context-managed WCS response."""

    status = 200

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        """Store a response body and headers."""
        self.body = body
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value

    def __enter__(self) -> Self:
        """Return the response as a context manager."""
        return self

    def read(self, amount: int = -1, /) -> bytes:
        """Read at most the requested response bytes."""
        return self.body if amount < 0 else self.body[:amount]

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the fake response without side effects."""
        return None


class RecordingProgress:
    """Record progress state changes without writing terminal output."""

    instances: ClassVar[list[RecordingProgress]] = []

    def __init__(self, *, total: int, **_kwargs: object) -> None:
        """Record the progress total and initialize an empty counter."""
        self.total = total
        self.count = 0
        self.descriptions: list[str] = []
        self.instances.append(self)

    def __enter__(self) -> Self:
        """Return this recorder as a progress context."""
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the progress recorder."""

    def set_description(self, description: str) -> None:
        """Record the current task description."""
        self.descriptions.append(description)

    def update(self, amount: int = 1) -> None:
        """Increment the completed-task count."""
        self.count += amount


def _valid_grib_message() -> bytes:
    message = bytearray(b"GRIB" + b"\0" * 12 + b"body7777")
    message[8:16] = len(message).to_bytes(8, "big")
    return bytes(message)


def _metadata(
    *,
    member: int = 0,
    lead: int = 0,
    date: int = 20261004,
) -> dict[str, int | float | str]:
    return {
        "paramId": 129,
        "typeOfLevel": "isobaricInhPa",
        "level": 500,
        "dataDate": date,
        "dataTime": 0,
        "step": lead,
        "number": member,
        "perturbationNumber": member,
        "numberOfForecastsInEnsemble": 35,
        "gridType": "regular_ll",
        "Ni": 1440,
        "Nj": 721,
        "units": "m**2 s**-2",
    }


def _capabilities(*initializations: str) -> bytes:
    coverages = "".join(
        f"<CoverageId>{wcs_retrieval.COVERAGE_PREFIX}{timestamp}</CoverageId>"
        for timestamp in initializations
    )
    return f"<Capabilities>{coverages}</Capabilities>".encode()


def test_required_leads_and_invalid_arguments() -> None:
    assert wcs_retrieval.required_leads(72, 24) == (72, 48, 24, 0)
    assert wcs_retrieval.required_leads(102, 24) == (102, 78, 54, 30, 6)
    with pytest.raises(ValueError, match="between 0 and 102"):
        wcs_retrieval.required_leads(103, 24)
    with pytest.raises(ValueError, match="positive"):
        wcs_retrieval.required_leads(24, 0)


def test_coverage_candidates_are_sorted_newest_first() -> None:
    candidates = wcs_retrieval.coverage_candidates(
        _capabilities("2026-10-03T18.00.00Z", "2026-10-04T00.00.00Z")
    )
    assert candidates[0][0].endswith("2026-10-04T00.00.00Z")
    assert candidates[1][0].endswith("2026-10-03T18.00.00Z")


def test_coverage_candidates_reject_malformed_xml_and_ids() -> None:
    with pytest.raises(RetrievalError, match="malformed XML"):
        wcs_retrieval.coverage_candidates(b"<Capabilities")
    malformed_capabilities = _capabilities("not-a-timestamp")
    with pytest.raises(RetrievalError, match="Invalid initialization"):
        wcs_retrieval.coverage_candidates(malformed_capabilities)


def test_get_capabilities_retries_at_service_directed_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    retry_time = datetime.now(UTC) + timedelta(minutes=2)
    error_body = json.dumps({"nextAccessTime": retry_time.isoformat()}).encode()
    throttle = HTTPError(
        "https://example.invalid",
        429,
        "Too Many Requests",
        Message(),
        io.BytesIO(error_body),
    )
    responses: list[HTTPError | FakeResponse] = [throttle, FakeResponse(b"<Capabilities />")]
    waits: list[float] = []
    wcs_retrieval._request_timestamps.clear()
    monkeypatch.setattr(wcs_retrieval, "_wait_for_request_slot", lambda _context: None)

    def open_request(_request: Request) -> FakeResponse:
        response = responses.pop(0)
        if isinstance(response, HTTPError):
            raise response
        return response

    monkeypatch.setattr(wcs_retrieval, "_open_https", open_request)
    monkeypatch.setattr(time, "sleep", waits.append)

    assert wcs_retrieval._get_capabilities("secret-token") == b"<Capabilities />"
    assert len(responses) == 0
    assert len(waits) == 1
    expected_delay = (retry_time - datetime.now(UTC)).total_seconds() + 1
    assert waits[0] == pytest.approx(expected_delay, abs=1)


def test_get_capabilities_does_not_retry_429_without_server_retry_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    throttle = HTTPError(
        "https://example.invalid",
        429,
        "Too Many Requests",
        Message(),
        io.BytesIO(b'{"message":"throttled"}'),
    )
    calls = 0
    wcs_retrieval._request_timestamps.clear()
    monkeypatch.setattr(wcs_retrieval, "_wait_for_request_slot", lambda _context: None)

    def open_request(_request: Request) -> FakeResponse:
        nonlocal calls
        calls += 1
        raise throttle

    monkeypatch.setattr(wcs_retrieval, "_open_https", open_request)
    monkeypatch.setattr(
        time,
        "sleep",
        lambda _delay: pytest.fail("must not sleep without a server retry time"),
    )

    with pytest.raises(RetrievalError, match=r"HTTP 429.*valid nextAccessTime"):
        wcs_retrieval._get_capabilities("secret-token")
    assert calls == 1


def test_wcs_requests_stay_below_rolling_minute_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    now = 100.0
    request_starts: list[float] = []
    waits: list[float] = []
    wcs_retrieval._request_timestamps.clear()

    def monotonic() -> float:
        return now

    def sleep(delay: float) -> None:
        nonlocal now
        waits.append(delay)
        now += delay

    def open_request(_request: Request) -> FakeResponse:
        nonlocal now
        request_starts.append(now)
        return FakeResponse(b"<Capabilities />")

    monkeypatch.setattr(time, "monotonic", monotonic)
    monkeypatch.setattr(time, "sleep", sleep)
    monkeypatch.setattr(wcs_retrieval, "_open_https", open_request)

    for _ in range(wcs_retrieval.MAX_REQUESTS_PER_MINUTE + 1):
        assert wcs_retrieval._get_capabilities("token") == b"<Capabilities />"

    assert len(request_starts) == wcs_retrieval.MAX_REQUESTS_PER_MINUTE + 1
    assert len(waits) == len(request_starts) - 1
    assert all(
        later - earlier >= wcs_retrieval.MIN_REQUEST_INTERVAL_SECONDS - 0.001
        for earlier, later in pairwise(request_starts)
    )
    assert request_starts[-1] - request_starts[0] >= wcs_retrieval.REQUEST_WINDOW_SECONDS
    assert all(
        sum(
            1
            for timestamp in request_starts
            if start <= timestamp <= start + wcs_retrieval.REQUEST_WINDOW_SECONDS
        )
        <= wcs_retrieval.MAX_REQUESTS_PER_MINUTE
        for start in request_starts
    )


def test_validate_grib_checks_field_identity_and_releases_handle() -> None:
    eccodes = FakeEccodes(_metadata(member=7, lead=24))
    metadata = wcs_retrieval._validate_grib(
        _valid_grib_message(),
        eccodes,
        member=7,
        lead_hours=24,
        initialization=datetime(2026, 10, 4, tzinfo=UTC),
    )
    assert metadata["number"] == 7
    assert eccodes.released

    mismatch = FakeEccodes(_metadata(member=8, lead=24))
    with pytest.raises(RetrievalError, match="metadata mismatch"):
        wcs_retrieval._validate_grib(
            _valid_grib_message(),
            mismatch,
            member=7,
            lead_hours=24,
            initialization=datetime(2026, 10, 4, tzinfo=UTC),
        )
    assert mismatch.released


def test_field_request_uses_api_header_and_exact_subsets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: list[Request] = []

    def open_request(request: Request, *, context: str) -> FakeResponse:
        captured.append(request)
        assert context == "GetCoverage member 007, lead 24 h"
        return FakeResponse(
            _valid_grib_message(),
            {"Content-Type": "application/wmo-grib"},
        )

    monkeypatch.setattr(wcs_retrieval, "_open_with_retries", open_request)
    response = wcs_retrieval._request_field(
        "test-secret",
        f"{wcs_retrieval.COVERAGE_PREFIX}2026-10-04T00.00.00Z",
        7,
        24,
    )
    request = captured[0]
    assert response == _valid_grib_message()
    assert request.full_url.startswith(wcs_retrieval.API_BASE_URL)
    assert "test-secret" not in request.full_url
    assert next(iter(request.unredirected_hdrs.values())) == "test-secret"
    query = parse_qs(urlsplit(request.full_url).query)
    assert query["subset"] == ["pressure(500)", "time(86400)"]
    assert "PEARP007" in request.full_url


def test_latest_complete_search_falls_back_and_writes_traceable_manifest(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """An advertised but incomplete newest run must not pass the completeness gate."""
    monkeypatch.setattr(wcs_retrieval, "MEMBERS", (0, 1))
    monkeypatch.setattr(wcs_retrieval, "read_token", lambda _path: "secret-token")
    monkeypatch.setattr(
        wcs_retrieval,
        "_get_capabilities",
        lambda _token: _capabilities("2026-10-03T18.00.00Z", "2026-10-04T00.00.00Z"),
    )
    monkeypatch.setattr(wcs_retrieval, "_load_eccodes", lambda: FakeEccodes(_metadata()))
    RecordingProgress.instances.clear()
    monkeypatch.setattr(wcs_retrieval, "tqdm", RecordingProgress)
    calls: list[tuple[str, int, int]] = []

    def request_field(_token: str, coverage_id: str, member: int, lead: int) -> bytes:
        calls.append((coverage_id, member, lead))
        if coverage_id.endswith("2026-10-04T00.00.00Z") and member == 0:
            message = "member unavailable"
            raise RetrievalError(message)
        return _valid_grib_message()

    def validate_grib(
        _body: bytes,
        _eccodes: object,
        *,
        member: int,
        lead_hours: int,
        initialization: datetime,
    ) -> dict[str, int | float | str]:
        return _metadata(
            member=member,
            lead=lead_hours,
            date=int(initialization.strftime("%Y%m%d")),
        )

    monkeypatch.setattr(wcs_retrieval, "_request_field", request_field)
    monkeypatch.setattr(wcs_retrieval, "_validate_grib", validate_grib)
    run_dir = wcs_retrieval.retrieve_latest_complete_run(
        RetrievalOptions(
            target_lead_hours=0,
            step_hours=24,
            output_dir=tmp_path,
            env_file=tmp_path / ".env",
        )
    )

    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["coverage_id"].endswith("2026-10-03T18.00.00Z")
    assert len(manifest["fields"]) == 2
    assert manifest["bytes_received"] == 2 * len(_valid_grib_message())
    assert (run_dir / "member_000_lead_000.grib").read_bytes() == _valid_grib_message()
    failed_manifest = json.loads(
        (tmp_path / "run_2026100400_t0_s24" / "manifest.json").read_text(encoding="utf-8")
    )
    assert failed_manifest["status"] == "failed"
    assert "member unavailable" in failed_manifest["failure"]
    assert failed_manifest["bytes_received"] == 0
    assert len(calls) == 3
    assert [progress.total for progress in RecordingProgress.instances] == [2, 2]
    assert [progress.count for progress in RecordingProgress.instances] == [0, 2]
    assert "Z500" in RecordingProgress.instances[1].descriptions[0]


def test_retrieval_revalidates_cached_fields_before_reuse(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(wcs_retrieval, "MEMBERS", (0,))
    monkeypatch.setattr(wcs_retrieval, "read_token", lambda _path: "secret-token")
    monkeypatch.setattr(
        wcs_retrieval,
        "_get_capabilities",
        lambda _token: _capabilities("2026-10-04T00.00.00Z"),
    )
    monkeypatch.setattr(wcs_retrieval, "_load_eccodes", lambda: FakeEccodes(_metadata()))
    requests: list[int] = []

    def request_field(_token: str, _coverage: str, member: int, _lead: int) -> bytes:
        requests.append(member)
        return _valid_grib_message()

    def validate_grib(
        body: bytes,
        _eccodes: object,
        *,
        member: int,
        lead_hours: int,
        initialization: datetime,
    ) -> dict[str, int | float | str]:
        if body != _valid_grib_message():
            message = "invalid cached field"
            raise RetrievalError(message)
        return _metadata(
            member=member,
            lead=lead_hours,
            date=int(initialization.strftime("%Y%m%d")),
        )

    monkeypatch.setattr(wcs_retrieval, "_request_field", request_field)
    monkeypatch.setattr(wcs_retrieval, "_validate_grib", validate_grib)
    options = RetrievalOptions(
        target_lead_hours=0,
        step_hours=24,
        output_dir=tmp_path,
        env_file=tmp_path / ".env",
    )
    run_dir = wcs_retrieval.retrieve_latest_complete_run(options)
    field_path = run_dir / "member_000_lead_000.grib"
    field_path.write_bytes(b"broken cache")

    reused_dir = wcs_retrieval.retrieve_latest_complete_run(options)
    manifest = json.loads((reused_dir / "manifest.json").read_text(encoding="utf-8"))
    assert reused_dir == run_dir
    assert len(requests) == 2
    assert manifest["status"] == "complete"
    assert manifest["fields"][0]["reused"] is False
    assert manifest["bytes_received"] == len(_valid_grib_message())
    assert field_path.read_bytes() == _valid_grib_message()


def test_visualization_requires_complete_run_manifest(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(
        json.dumps({"status": "in_progress", "fields": []}),
        encoding="utf-8",
    )
    with pytest.raises(RetrievalError, match="not complete"):
        visualization.render_quicklook(tmp_path, lead_hours=0)


def test_quicklook_renders_one_field_and_writes_provenance(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(GeoAxes, "coastlines", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(GeoAxes, "add_feature", lambda *_args, **_kwargs: None)
    original_imshow = GeoAxes.imshow
    plot_references: list[tuple[object, object]] = []
    plot_extents: list[tuple[float, ...]] = []

    def capture_imshow(
        axes: GeoAxes,
        *args: object,
        **kwargs: object,
    ) -> object:
        plot_references.append((axes.projection, kwargs.get("transform")))
        plot_extents.append(cast("tuple[float, ...]", kwargs["extent"]))
        return original_imshow(axes, *args, **kwargs)

    monkeypatch.setattr(GeoAxes, "imshow", capture_imshow)
    initialization = datetime(2026, 10, 4, tzinfo=UTC)
    manifest = {
        "status": "complete",
        "coverage_id": f"{wcs_retrieval.COVERAGE_PREFIX}2026-10-04T00.00.00Z",
        "initialization_utc": initialization.isoformat(),
        "fields": [
            {
                "member": 0,
                "lead_hours": 0,
                "relative_path": "member_000_lead_000.grib",
            }
        ],
    }
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest),
        encoding="utf-8",
    )
    (tmp_path / "member_000_lead_000.grib").write_bytes(_valid_grib_message())
    monkeypatch.setattr(
        visualization,
        "_load_eccodes",
        lambda: FakeEccodes(_metadata()),
    )
    output_path = visualization.render_quicklook(tmp_path, lead_hours=0)

    assert output_path.is_file()
    assert output_path.stat().st_size > 0
    sidecar = json.loads(output_path.with_suffix(".json").read_text(encoding="utf-8"))
    assert sidecar["source_credit"] == "Source : Météo-France"
    assert sidecar["field_minimum"] == pytest.approx(54_000.0)
    assert sidecar["field_maximum"] == pytest.approx(54_000.0)
    assert len(sidecar["output_sha256"]) == 64
    assert len(plot_references) == 1
    assert isinstance(plot_references[0][0], ccrs.PlateCarree)
    assert isinstance(plot_references[0][1], ccrs.PlateCarree)
    assert plot_extents == [(-180.125, 179.875, -90.125, 90.125)]


def test_coordinates_are_validated_and_recentered_at_antimeridian() -> None:
    values, longitudes, latitudes = visualization._decode_arrays(
        _valid_grib_message(),
        FakeEccodes(_metadata()),
    )
    assert values.shape == (721, 1440)
    assert longitudes[0] == pytest.approx(-180.0)
    assert longitudes[-1] == pytest.approx(179.75)
    assert latitudes[0] == pytest.approx(90.0)
    assert latitudes[-1] == pytest.approx(-90.0)


def test_coordinate_verification_rejects_misaligned_latitudes() -> None:
    class MisalignedGridEccodes(FakeEccodes):
        @override
        @staticmethod
        def codes_get_array(_handle: object, key: str) -> object:
            if key == "latitudes":
                latitudes = np.repeat(
                    90.0 - np.arange(721, dtype=np.float64) * 0.25,
                    1440,
                )
                latitudes[0] = 89.75
                return latitudes
            return FakeEccodes.codes_get_array(_handle, key)

    with pytest.raises(RetrievalError, match="latitude coordinates"):
        visualization._decode_arrays(
            _valid_grib_message(),
            MisalignedGridEccodes(_metadata()),
        )


def test_selection_rejects_invalid_wildcard_and_duplicate_values() -> None:
    with pytest.raises(ValueError, match="cannot be combined"):
        visualization._parse_selection(
            "*,2",
            (0, 1, 2),
            label="members",
            minimum=0,
            maximum=34,
        )
    with pytest.raises(ValueError, match="duplicates"):
        visualization._parse_selection(
            "2,2",
            (0, 1, 2),
            label="members",
            minimum=0,
            maximum=34,
        )


def test_batch_quicklooks_support_wildcards_and_track_each_map(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    fields = [
        {
            "member": member,
            "lead_hours": lead,
            "relative_path": f"member_{member:03}_lead_{lead:03}.grib",
        }
        for lead in (24, 0)
        for member in range(35)
    ]
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "initialization_utc": datetime(2026, 10, 4, tzinfo=UTC).isoformat(),
                "fields": fields,
            }
        ),
        encoding="utf-8",
    )
    contexts: list[tuple[int, int, Path]] = []

    def load_context(
        selected_run_dir: Path,
        *,
        lead_hours: int,
        member: int,
        output_path: Path | None = None,
    ) -> visualization._QuicklookContext:
        assert output_path is not None
        contexts.append((lead_hours, member, output_path))
        return visualization._QuicklookContext(
            run_dir=selected_run_dir,
            output_path=output_path,
            manifest={"coverage_id": "test"},
            initialization=datetime(2026, 10, 4, tzinfo=UTC),
            member=member,
            lead_hours=lead_hours,
            metadata=_metadata(member=member, lead=lead_hours),
            values=np.zeros((1, 1), dtype=np.float32),
            longitude_axis=np.zeros(1, dtype=np.float64),
            latitude_axis=np.zeros(1, dtype=np.float64),
        )

    def render_png(context: visualization._QuicklookContext) -> None:
        context.output_path.parent.mkdir(parents=True, exist_ok=True)
        context.output_path.write_bytes(b"png")

    monkeypatch.setattr(visualization, "_load_quicklook_context", load_context)
    monkeypatch.setattr(visualization, "_render_png", render_png)
    monkeypatch.setattr(visualization, "_write_provenance", lambda _context: None)
    RecordingProgress.instances.clear()
    monkeypatch.setattr(visualization, "tqdm", RecordingProgress)

    maps = visualization.render_quicklooks(
        run_dir,
        lead_hours="*",
        members="*",
        output_dir=tmp_path / "maps",
    )

    assert len(maps) == 70
    assert [path.name for path in maps[:3]] == [
        "z500_member_000_lead_024.png",
        "z500_member_001_lead_024.png",
        "z500_member_002_lead_024.png",
    ]
    assert maps[-1].name == "z500_member_034_lead_000.png"
    assert contexts[0][:2] == (24, 0)
    assert contexts[34][:2] == (24, 34)
    assert contexts[35][:2] == (0, 0)
    assert contexts[-1][:2] == (0, 34)
    progress = RecordingProgress.instances[0]
    assert progress.total == progress.count == 70
    assert progress.descriptions[0] == "Quicklook Z500 | t+24h | member 000"
    assert progress.descriptions[35] == "Quicklook Z500 | t+0h | member 000"
    assert progress.descriptions[-1] == "Quicklook Z500 | t+0h | member 034"
