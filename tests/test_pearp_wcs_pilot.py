# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for PEARP WCS pilot selection, retries, and field validation."""

from __future__ import annotations

import importlib
import io
import json
import logging
import sys
import time
from datetime import UTC, datetime, timedelta
from email.message import Message
from typing import TYPE_CHECKING, Self
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from docs import pilot_pearp_wcs as pilot
from docs.pilot_pearp_wcs import (
    FieldRequest,
    FieldResult,
    PilotError,
    PilotOptions,
    RequestMetrics,
    latest_coverage_id,
    next_access_time,
    required_leads,
    validate_metadata,
)
from docs.probe_pe_arpege_api import VARIABLE_NAME

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path
    from types import TracebackType


class FakeResponse:
    """Small context-managed HTTP response for deterministic tests."""

    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        """Initialize the response body and headers."""
        self.body = body
        self.headers = Message()
        for key, value in (headers or {}).items():
            self.headers[key] = value
        self.status = 200

    def __enter__(self) -> Self:
        """Return this response for a context-managed request."""
        return self

    def read(self, amount: int = -1, /) -> bytes:
        """Return at most the requested number of response bytes."""
        return self.body if amount < 0 else self.body[:amount]

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the fake response without side effects."""


class FakeEccodes:
    """Return deterministic GRIB metadata and track handle release."""

    def __init__(self, metadata: dict[str, int | float | str]) -> None:
        """Store metadata for the fake GRIB handle."""
        self.metadata = metadata
        self.released = False

    def codes_get(self, _handle: object, key: str) -> int | float | str:
        """Return one deterministic metadata value."""
        return self.metadata[key]

    @staticmethod
    def codes_new_from_message(_message: bytes) -> object:
        """Create an opaque stand-in for an ecCodes handle."""
        return object()

    def codes_release(self, _handle: object) -> None:
        """Record that the fake handle was released."""
        self.released = True


def _http_error(code: int, body: bytes) -> HTTPError:
    """Build an HTTPError carrying the supplied test body."""
    return HTTPError("https://example.invalid", code, "failure", Message(), io.BytesIO(body))


def _valid_grib_message() -> bytes:
    """Build a minimally complete GRIB-framed byte string."""
    message = bytearray(b"GRIB" + b"\0" * 12 + b"body7777")
    message[8:16] = len(message).to_bytes(8, "big")
    return bytes(message)


def _valid_metadata() -> dict[str, int | float | str]:
    """Return Z500 metadata matching the expected run/member/lead."""
    return {
        "paramId": 129,
        "typeOfLevel": "isobaricInhPa",
        "level": 500,
        "dataDate": 20261004,
        "dataTime": 0,
        "step": 24,
        "number": 7,
        "perturbationNumber": 7,
        "numberOfForecastsInEnsemble": 35,
        "gridType": "regular_ll",
        "Ni": 1440,
        "Nj": 721,
        "units": "m**2 s**-2",
    }


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    """Create a token file for CLI tests."""
    path = tmp_path / ".env"
    path.write_text(f"{VARIABLE_NAME}=test-token\n", encoding="utf-8")
    return path


@pytest.mark.parametrize(
    ("target", "step", "expected"),
    [
        (24, 24, [24, 0]),
        (72, 24, [72, 48, 24, 0]),
        (102, 24, [102, 78, 54, 30, 6]),
    ],
)
def test_required_leads(target: int, step: int, expected: list[int]) -> None:
    """Return target and preceding leads at the analysis interval."""
    assert required_leads(target, step) == expected


@pytest.mark.parametrize(
    ("target", "step", "message"),
    [
        (-1, 24, "negative target"),
        (103, 24, "API maximum"),
        (24, 0, "non-positive"),
    ],
)
def test_required_leads_reject_invalid_arguments(target: int, step: int, message: str) -> None:
    """Reject target leads outside the valid API range."""
    with pytest.raises(ValueError, match=message):
        required_leads(target, step)


def test_latest_coverage_selects_newest_z500_and_ignores_other_variables() -> None:
    """Select the newest isobaric geopotential coverage."""
    xml = b"""<Capabilities>
      <CoverageId>GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-03T18.00.00Z</CoverageId>
      <CoverageId>GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-04T00.00.00Z</CoverageId>
      <CoverageId>TEMPERATURE__ISOBARIC_SURFACE___2026-10-04T06.00.00Z</CoverageId>
    </Capabilities>"""
    assert latest_coverage_id(xml).endswith("2026-10-04T00.00.00Z")


@pytest.mark.parametrize(
    ("xml", "message"),
    [
        (b"<Capabilities />", "No isobaric geopotential"),
        (
            b"<Capabilities><CoverageId>"
            b"GEOPOTENTIAL__ISOBARIC_SURFACE___" + b"invalid"
            b"</CoverageId></Capabilities>",
            "Invalid initialization timestamp",
        ),
    ],
)
def test_latest_coverage_rejects_missing_or_malformed_data(xml: bytes, message: str) -> None:
    """Fail clearly when no valid candidate initialization exists."""
    with pytest.raises(PilotError, match=message):
        latest_coverage_id(xml)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            '{"nextAccessTime":"2026-10-04T11:05:00+0000"}',
            datetime(2026, 10, 4, 11, 5, tzinfo=UTC),
        ),
        (
            '{"nextAccessTime":"2026-oct.-04 11:05:00+0000 UTC"}',
            datetime(2026, 10, 4, 11, 5, tzinfo=UTC),
        ),
        ("no retry time", None),
        ('{"nextAccessTime":"invalid"}', None),
    ],
)
def test_next_access_time_parses_service_formats(body: str, expected: datetime | None) -> None:
    """Parse retry timestamps and ignore missing or invalid values."""
    assert next_access_time(body) == expected


def test_validate_metadata_accepts_requested_field() -> None:
    """Accept a matching member, run, lead, pressure, grid, and unit."""
    validate_metadata(
        _valid_metadata(),
        member=7,
        lead_hours=24,
        initialization=datetime(2026, 10, 4, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [("number", 6), ("dataDate", 20261003), ("units", "m")],
)
def test_validate_metadata_rejects_mismatches(key: str, value: int | str) -> None:
    """Reject wrong member, run, or scientific units."""
    metadata = {**_valid_metadata(), key: value}
    initialization = datetime(2026, 10, 4, tzinfo=UTC)
    with pytest.raises(PilotError, match="metadata mismatch"):
        validate_metadata(
            metadata,
            member=7,
            lead_hours=24,
            initialization=initialization,
        )


def test_decode_single_message_validates_and_releases_handle() -> None:
    """Decode one complete GRIB message and release ecCodes resources."""
    eccodes = FakeEccodes(_valid_metadata())
    assert pilot._decode_single_message(_valid_grib_message(), eccodes) == _valid_metadata()
    assert eccodes.released


@pytest.mark.parametrize(
    "body",
    [b"not-grib", _valid_grib_message()[:-1] + b"x", b"GRIB" + b"\0" * 20 + b"7777"],
)
def test_decode_single_message_rejects_invalid_framing(body: bytes) -> None:
    """Reject invalid magic, trailer, or declared message length."""
    eccodes = FakeEccodes(_valid_metadata())
    with pytest.raises(PilotError, match=r"complete GRIB message|declares"):
        pilot._decode_single_message(body, eccodes)


def test_load_eccodes_explains_missing_optional_binding(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explain how to install the optional ecCodes binding."""
    monkeypatch.setattr(
        importlib,
        "import_module",
        lambda _name: (_ for _ in ()).throw(ImportError("missing")),
    )
    with pytest.raises(PilotError, match="requires ecCodes"):
        pilot._load_eccodes()


def test_get_capabilities_accepts_xml_and_rejects_non_xml(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Return XML capabilities and reject malformed service output."""
    monkeypatch.setattr(pilot, "urlopen", lambda *_args, **_kwargs: FakeResponse(b"<WCS />"))
    assert pilot._get_capabilities("token") == b"<WCS />"
    monkeypatch.setattr(pilot, "urlopen", lambda *_args, **_kwargs: FakeResponse(b"no xml"))
    with pytest.raises(PilotError, match="not XML"):
        pilot._get_capabilities("token")


def test_throttle_retry_waits_for_service_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Use the supplied retry time and continue after a 429."""
    retry_time = (datetime.now(UTC) + timedelta(seconds=2)).isoformat()
    failure = _http_error(429, json.dumps({"nextAccessTime": retry_time}).encode())
    response = FakeResponse(b"ok")
    results: Iterator[HTTPError | FakeResponse] = iter((failure, response))

    def fake_urlopen(*_args: object, **_kwargs: object) -> FakeResponse:
        result = next(results)
        if isinstance(result, HTTPError):
            raise result
        return result

    sleeps: list[float] = []
    monkeypatch.setattr(pilot, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    opened, retries, waited, network_retries = pilot._open_with_retries(
        Request("https://example.invalid"),
        7,
        24,
    )
    assert opened is response
    assert retries == 1
    assert waited >= 1
    assert sleeps == [waited]
    assert network_retries == 0


@pytest.mark.parametrize(
    ("code", "body", "message"),
    [(404, b"missing", "HTTP 404"), (429, b"not json", "HTTP 429")],
)
def test_throttle_retry_rejects_non_retryable_http_error(
    monkeypatch: pytest.MonkeyPatch,
    code: int,
    body: bytes,
    message: str,
) -> None:
    """Reject ordinary HTTP errors and throttles without retry metadata."""

    def fail_urlopen(*_args: object, **_kwargs: object) -> FakeResponse:
        raise _http_error(code, body)

    monkeypatch.setattr(pilot, "urlopen", fail_urlopen)
    request = Request("https://example.invalid")
    with pytest.raises(PilotError, match=message):
        pilot._open_with_retries(request, 0, 24)


def test_network_retry_handles_transient_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Retry timeout and OS errors, then return the successful response."""
    response = FakeResponse(b"ok")
    results: Iterator[BaseException | FakeResponse] = iter(
        (TimeoutError("first"), OSError("second"), response)
    )

    def fake_urlopen(*_args: object, **_kwargs: object) -> FakeResponse:
        result = next(results)
        if isinstance(result, BaseException):
            raise result
        return result

    sleeps: list[float] = []
    monkeypatch.setattr(pilot, "urlopen", fake_urlopen)
    monkeypatch.setattr(time, "sleep", sleeps.append)
    opened, throttles, throttle_wait, retries = pilot._open_with_retries(
        Request("https://example.invalid"),
        0,
        24,
    )
    assert opened is response
    assert sleeps == [1.0, 2.0]
    assert retries == 2
    assert throttles == 0
    assert throttle_wait == 0


def test_network_retry_exhaustion_and_url_error_are_reported(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Report exhausted retries and non-retryable URL failures."""
    monkeypatch.setattr(
        pilot,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(TimeoutError("offline")),
    )
    monkeypatch.setattr(time, "sleep", lambda _delay: None)
    request = Request("https://example.invalid")
    with pytest.raises(PilotError, match="after 2 retries"):
        pilot._open_with_retries(request, 0, 24)
    monkeypatch.setattr(
        pilot,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with pytest.raises(PilotError, match="Network failure"):
        pilot._open_with_retries(request, 0, 24)


def test_validate_response_checks_size_content_type_and_grib_metadata() -> None:
    """Accept valid metadata and reject oversized, XML, or mismatched responses."""
    response = FakeResponse(_valid_grib_message(), {"Content-Type": "application/wmo-grib"})
    field = FieldRequest("coverage", 7, 24, datetime(2026, 10, 4, tzinfo=UTC))
    metrics = RequestMetrics(time.monotonic(), 0, 0.0, 0)
    matching_metadata = FakeEccodes(_valid_metadata())
    result = pilot._validate_response(response, field, matching_metadata, metrics)
    assert result.bytes_received == len(_valid_grib_message())
    oversized = FakeResponse(b"x", {"Content-Length": str(pilot.MAX_RESPONSE_BYTES + 1)})
    oversized_metadata = FakeEccodes(_valid_metadata())
    with pytest.raises(PilotError, match="Content-Length"):
        pilot._validate_response(oversized, field, oversized_metadata, metrics)
    xml = FakeResponse(b"<error/>", {"Content-Type": "application/xml"})
    xml_metadata = FakeEccodes(_valid_metadata())
    with pytest.raises(PilotError, match="Expected GRIB"):
        pilot._validate_response(xml, field, xml_metadata, metrics)
    mismatch = FakeEccodes({**_valid_metadata(), "step": 3})
    with pytest.raises(PilotError, match="metadata mismatch"):
        pilot._validate_response(response, field, mismatch, metrics)


def test_request_field_builds_requested_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Build the expected member endpoint and pressure/time subsets."""
    captured: list[Request] = []
    route: list[tuple[int, int]] = []
    response = FakeResponse(_valid_grib_message())
    result = FieldResult(7, 24, 200, "application/wmo-grib", 32, 0.1, 0, 0, 0, {})

    def fake_open(
        request: Request,
        member: int,
        lead: int,
    ) -> tuple[FakeResponse, int, float, int]:
        captured.append(request)
        route.append((member, lead))
        return response, 0, 0.0, 0

    monkeypatch.setattr(pilot, "_open_with_retries", fake_open)
    monkeypatch.setattr(pilot, "_validate_response", lambda *_args: result)
    request = FieldRequest("coverage", 7, 24, datetime(2026, 10, 4, tzinfo=UTC))
    assert pilot._request_field("token", request, FakeEccodes(_valid_metadata())) is result
    built_request = captured[0]
    assert "PEARP007-" in built_request.full_url
    assert "pressure%28500%29" in built_request.full_url
    assert "time%2886400%29" in built_request.full_url
    assert route == [(7, 24)]


def test_run_pilot_writes_complete_measurement_report(
    monkeypatch: pytest.MonkeyPatch,
    token_file: Path,
    tmp_path: Path,
) -> None:
    """Write complete batch measurements without network or ecCodes."""
    requested: list[tuple[int, int]] = []
    result = FieldResult(0, 24, 200, "application/wmo-grib", 100, 0.2, 0, 0, 0, {})
    monkeypatch.setattr(pilot, "read_token", lambda _path: "token")
    monkeypatch.setattr(pilot, "_get_capabilities", lambda _token: b"<Capabilities />")
    monkeypatch.setattr(
        pilot,
        "latest_coverage_id",
        lambda _body: "GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-04T00.00.00Z",
    )
    monkeypatch.setattr(pilot, "_load_eccodes", lambda: FakeEccodes(_valid_metadata()))

    def request_field(
        _token: str,
        request: FieldRequest,
        _eccodes: FakeEccodes,
    ) -> FieldResult:
        requested.append((request.member, request.lead_hours))
        return result

    monkeypatch.setattr(pilot, "_request_field", request_field)
    report_path = tmp_path / "report.json"
    assert pilot.run_pilot(PilotOptions(24, 24, report_path, token_file)) == 0
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["complete"] is True
    assert report["requests_expected"] == report["requests_completed"] == 70
    assert report["bytes_received"] == 7000
    assert len(requested) == 70


def test_run_pilot_records_failure_and_stops(
    monkeypatch: pytest.MonkeyPatch,
    token_file: Path,
    tmp_path: Path,
) -> None:
    """Record incomplete work and do not continue after a required field fails."""
    calls = 0
    monkeypatch.setattr(pilot, "read_token", lambda _path: "token")
    monkeypatch.setattr(pilot, "_get_capabilities", lambda _token: b"<Capabilities />")
    monkeypatch.setattr(
        pilot,
        "latest_coverage_id",
        lambda _body: "GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-04T00.00.00Z",
    )
    monkeypatch.setattr(pilot, "_load_eccodes", lambda: FakeEccodes(_valid_metadata()))

    def fail_request(*_args: object) -> FieldResult:
        nonlocal calls
        calls += 1
        failure_message = "field unavailable"
        raise PilotError(failure_message)

    monkeypatch.setattr(pilot, "_request_field", fail_request)
    report_path = tmp_path / "incomplete.json"
    result = pilot.run_pilot(PilotOptions(24, 24, report_path, token_file))
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert result == 1
    assert report["complete"] is False
    assert report["failure"] == "field unavailable"
    assert report["requests_completed"] == 0
    assert calls == 1


def test_main_validates_arguments_and_runs_pilot(
    monkeypatch: pytest.MonkeyPatch,
    token_file: Path,
) -> None:
    """Reject an excessive target lead and dispatch valid command-line options."""
    monkeypatch.setattr(sys, "argv", ["pilot", "--target-lead-hours", "103"])
    with pytest.raises(SystemExit):
        pilot.main()
    monkeypatch.setattr(
        sys,
        "argv",
        ["pilot", "--env-file", str(token_file), "--target-lead-hours", "24"],
    )
    monkeypatch.setattr(pilot, "run_pilot", lambda _options: 0)
    assert pilot.main() == 0


def test_main_logs_run_failure(
    monkeypatch: pytest.MonkeyPatch,
    token_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Log execution errors and return a failure exit code."""
    monkeypatch.setattr(sys, "argv", ["pilot", "--env-file", str(token_file)])
    monkeypatch.setattr(
        pilot,
        "run_pilot",
        lambda _options: (_ for _ in ()).throw(PilotError("failed")),
    )
    with caplog.at_level(logging.ERROR):
        assert pilot.main() == 1
    assert "Unable to run WCS pilot" in caplog.text
