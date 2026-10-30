# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""End-to-end tests of the ``fetch`` command with real GRIB messages and a faked HTTPS API."""

from __future__ import annotations

import json
import logging
import re
from datetime import UTC, datetime
from email.message import Message
from typing import TYPE_CHECKING, Self
from urllib.error import HTTPError
from urllib.parse import parse_qs, urlsplit

import eccodes
import numpy as np
import pytest

from ensemble_sensitivity.data_retrieval import wcs_retrieval
from ensemble_sensitivity.main import main

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType
    from urllib.request import Request

GRID_NI = 1440
GRID_NJ = 721
MEMBER_COUNT = 35
NEWEST_RUN = "2026-10-04T00.00.00Z"
OLDER_RUN = "2026-10-03T12.00.00Z"
RUN_DIR_PATTERN = re.compile(r"run_\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-Z_lead_time_48_step_24")


class _Response:
    def __init__(self, body: bytes, content_type: str) -> None:
        self.body = body
        self.status = 200
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        self.headers["Content-Length"] = str(len(body))

    def __enter__(self) -> Self:
        return self

    def read(self, amount: int = -1, /) -> bytes:
        return self.body if amount < 0 else self.body[:amount]

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None


def _grib_message(*, member: int, lead_hours: int, run: str) -> bytes:
    """Build a constant-valued global Z500 GRIB2 message carrying the PEARP metadata.

    Returns:
        Encoded GRIB message.

    """
    initialization = datetime.strptime(run, "%Y-%m-%dT%H.%M.%SZ").replace(tzinfo=UTC)
    handle = eccodes.codes_grib_new_from_samples("regular_ll_pl_grib2")
    try:
        for key, value in (
            ("productDefinitionTemplateNumber", 1),
            ("Ni", GRID_NI),
            ("Nj", GRID_NJ),
            ("latitudeOfFirstGridPointInDegrees", 90.0),
            ("latitudeOfLastGridPointInDegrees", -90.0),
            ("longitudeOfFirstGridPointInDegrees", 0.0),
            ("longitudeOfLastGridPointInDegrees", 359.75),
            ("iDirectionIncrementInDegrees", 0.25),
            ("jDirectionIncrementInDegrees", 0.25),
            ("paramId", 129),
            ("typeOfLevel", "isobaricInhPa"),
            ("level", 500),
            ("dataDate", int(initialization.strftime("%Y%m%d"))),
            ("dataTime", initialization.hour * 100),
            ("stepUnits", 1),
            ("step", lead_hours),
            ("numberOfForecastsInEnsemble", MEMBER_COUNT),
            ("perturbationNumber", member),
        ):
            eccodes.codes_set(handle, key, value)
        eccodes.codes_set_values(handle, np.full(GRID_NI * GRID_NJ, 54000.0 + member))
        return bytes(eccodes.codes_get_message(handle))
    finally:
        eccodes.codes_release(handle)


class _FakeWcsApi:
    """Serve capabilities and GetCoverage responses, optionally failing for one run."""

    def __init__(self, runs: list[str], *, broken_runs: frozenset[str] = frozenset()) -> None:
        self.runs = runs
        self.broken_runs = broken_runs
        self.coverage_requests: list[tuple[int, str, int]] = []

    def __call__(self, request: Request, timeout: float) -> _Response:  # ruff: ignore[unused-method-argument]
        url = urlsplit(request.full_url)
        if url.path.endswith("GetCapabilities"):
            coverages = "".join(
                f"<CoverageId>{wcs_retrieval.COVERAGE_PREFIX}{run}</CoverageId>"
                for run in self.runs
            )
            return _Response(f"<Capabilities>{coverages}</Capabilities>".encode(), "text/xml")
        return self._coverage(request, url.path, parse_qs(url.query))

    def _coverage(self, request: Request, path: str, query: dict[str, list[str]]) -> _Response:
        member_match = re.search(r"PEARP(\d{3})", path)
        assert member_match is not None
        member = int(member_match.group(1))
        run = query["coverageid"][0].removeprefix(wcs_retrieval.COVERAGE_PREFIX)
        time_subset = next(v for v in query["subset"] if v.startswith("time("))
        lead_hours = int(time_subset.removeprefix("time(").removesuffix(")")) // 3600
        assert query["subset"][0] == "pressure(500)"
        assert request.get_header("Apikey") == "test-token"
        self.coverage_requests.append((member, run, lead_hours))
        if run in self.broken_runs:
            raise HTTPError(request.full_url, 404, "Not Found", Message(), None)
        return _Response(
            _grib_message(member=member, lead_hours=lead_hours, run=run),
            "application/wmo-grib",
        )


@pytest.fixture
def fake_api(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _FakeWcsApi:
    api = _FakeWcsApi([OLDER_RUN, NEWEST_RUN])
    monkeypatch.setattr(wcs_retrieval, "urlopen", api)
    monkeypatch.setattr(wcs_retrieval, "MIN_REQUEST_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(wcs_retrieval, "sleep", lambda _seconds: None)
    wcs_retrieval._request_timestamps.clear()  # ruff: ignore[private-member-access]
    monkeypatch.setenv(wcs_retrieval.VARIABLE_NAME, "test-token")
    monkeypatch.chdir(tmp_path)
    return api


def test_fetch_writes_usable_raw_grib_fields(
    fake_api: _FakeWcsApi,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    main(["fetch", "-l", "48", "-s", "24", "-q"])

    (run_dir,) = (tmp_path / "output").iterdir()
    assert RUN_DIR_PATTERN.fullmatch(run_dir.name)
    assert [path.name for path in run_dir.iterdir()] == ["raw_fetch"]
    raw_fetch = run_dir / "raw_fetch"
    fields = sorted(raw_fetch.glob("member_*_lead_*.grib"))
    assert len(fields) == MEMBER_COUNT * 3
    manifest = json.loads((raw_fetch / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["status"] == "complete"
    assert manifest["coverage_id"].endswith(NEWEST_RUN)
    assert len(manifest["fields"]) == len(fields)
    assert {lead for _, _, lead in fake_api.coverage_requests} == {0, 24, 48}
    assert not capsys.readouterr().out

    with (raw_fetch / "member_007_lead_048.grib").open("rb") as stream:
        handle = eccodes.codes_grib_new_from_file(stream)
    try:
        assert eccodes.codes_get(handle, "step") == 48
        assert eccodes.codes_get(handle, "perturbationNumber") == 7
        assert eccodes.codes_get(handle, "level") == 500
        assert eccodes.codes_get_values(handle)[0] == pytest.approx(54007.0)
    finally:
        eccodes.codes_release(handle)


def test_fetch_falls_back_to_previous_run_when_newest_is_incomplete(
    fake_api: _FakeWcsApi,
    tmp_path: Path,
) -> None:
    fake_api.broken_runs = frozenset({NEWEST_RUN})

    main(["fetch", "-l", "24", "-s", "24", "-q"])

    (run_dir,) = (tmp_path / "output").iterdir()
    manifest = json.loads((run_dir / "raw_fetch" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["coverage_id"].endswith(OLDER_RUN)
    assert sorted(path.name for path in run_dir.iterdir()) == ["raw_fetch"]


def test_fetch_failure_exits_with_error_and_leaves_no_output(
    fake_api: _FakeWcsApi,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    fake_api.broken_runs = frozenset({NEWEST_RUN, OLDER_RUN})

    with caplog.at_level(logging.ERROR), pytest.raises(SystemExit) as exit_info:
        main(["fetch", "-l", "24", "-s", "24", "-q"])

    assert exit_info.value.code == 1
    assert "Unable to retrieve a complete PEARP run" in caplog.text
    assert list((tmp_path / "output").iterdir()) == []


def test_fetch_rejects_hours_without_published_z500(
    fake_api: _FakeWcsApi,
    tmp_path: Path,
) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main(["fetch", "-l", "25", "-s", "24", "-q"])

    assert exit_info.value.code == 1
    assert fake_api.coverage_requests == []
    assert not (tmp_path / "output").exists()
