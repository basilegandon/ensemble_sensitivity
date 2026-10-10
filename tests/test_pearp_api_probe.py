# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the PE-ARPEGE API exploration probe."""

from __future__ import annotations

import io
import logging
import sys
from email.message import Message
from typing import TYPE_CHECKING, Self
from urllib.error import HTTPError, URLError
from xml.etree import ElementTree as ET

import pytest

from docs.PEARP_data import probe_pe_arpege_api as probe

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType
    from urllib.request import Request


class FakeResponse:
    """Small context-managed HTTP response for deterministic probe tests."""

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


def _http_error(code: int, body: bytes, headers: dict[str, str] | None = None) -> HTTPError:
    """Build an HTTPError carrying the supplied test body and headers."""
    message = Message()
    for key, value in (headers or {}).items():
        message[key] = value
    return HTTPError("https://example.invalid", code, "failure", message, io.BytesIO(body))


@pytest.fixture
def token_file(tmp_path: Path) -> Path:
    """Create a token file for CLI and parser tests."""
    path = tmp_path / ".env"
    path.write_text(f'export {probe.VARIABLE_NAME}="secret-token"\n', encoding="utf-8")
    return path


def test_read_token_parses_comments_export_and_quotes(tmp_path: Path) -> None:
    path = tmp_path / ".env"
    path.write_text(
        f"# ignored\nexport {probe.VARIABLE_NAME} = 'the-token'\n",
        encoding="utf-8",
    )
    assert probe.read_token(path) == "the-token"


@pytest.mark.parametrize("contents", ["", "# empty\n", f"{probe.VARIABLE_NAME}=\n"])
def test_read_token_rejects_missing_or_empty_value(tmp_path: Path, contents: str) -> None:
    path = tmp_path / ".env"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(ValueError, match="missing or empty"):
        probe.read_token(path)


def test_read_token_propagates_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match=r"missing\.env"):
        probe.read_token(tmp_path / "missing.env")


def test_request_xml_returns_authenticated_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = FakeResponse(b"<Capabilities />")
    requests: list[Request] = []
    timeouts: list[int] = []

    def fake_urlopen(request: Request, *, timeout: int) -> FakeResponse:
        requests.append(request)
        timeouts.append(timeout)
        return response

    monkeypatch.setattr(probe, "urlopen", fake_urlopen)
    result = probe.request_xml("secret", "003", "GetCapabilities", {"service": "WCS"})
    assert result == b"<Capabilities />"
    request = requests[0]
    assert request.get_header("Apikey") == "secret"
    assert timeouts == [30]


def test_request_xml_redacts_http_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail_urlopen(*_args: object, **_kwargs: object) -> FakeResponse:
        raise _http_error(403, b"secret denied")

    monkeypatch.setattr(probe, "urlopen", fail_urlopen)
    with caplog.at_level(logging.ERROR):
        assert probe.request_xml("secret", "000", "GetCapabilities", {}) is None
    logged_message = caplog.records[0].getMessage()
    assert "secret" not in logged_message
    assert probe.REDACTED_VALUE in logged_message


def test_request_xml_handles_network_error(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        probe,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(URLError("offline")),
    )
    with caplog.at_level(logging.ERROR):
        assert probe.request_xml("secret", "000", "GetCapabilities", {}) is None
    assert "Unable to reach" in caplog.text


def test_read_coverage_response_accepts_bounded_grib() -> None:
    assert probe._read_coverage_response(FakeResponse(b"GRIB"), "secret", 8) == b"GRIB"


@pytest.mark.parametrize(
    ("response", "limit", "expected_log"),
    [
        (FakeResponse(b"GRIB", {"Content-Length": "9"}), 8, "Content-Length"),
        (FakeResponse(b"123456789"), 8, "exceeds limit"),
        (
            FakeResponse(b"<xml>secret</xml>", {"Content-Type": "application/xml"}),
            100,
            "Expected GRIB",
        ),
    ],
)
def test_read_coverage_response_rejects_invalid_or_oversized_content(
    response: FakeResponse,
    limit: int,
    expected_log: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        result = probe._read_coverage_response(response, "secret", limit)
    assert result is None
    assert expected_log in caplog.text
    assert "secret" not in caplog.text


def test_request_coverage_writes_bounded_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    output = tmp_path / "nested" / "sample.grib"
    monkeypatch.setattr(probe, "urlopen", lambda *_args, **_kwargs: FakeResponse(b"GRIB"))
    result = probe.request_coverage(
        "secret",
        probe._CoverageRequest("002", "coverage", ["pressure(500)"], output, 8),
    )
    assert result == 0
    assert output.read_bytes() == b"GRIB"


@pytest.mark.parametrize(
    "failure",
    [
        _http_error(400, b"secret failed"),
        URLError("offline"),
    ],
)
def test_request_coverage_returns_failure_for_transport_errors(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: BaseException,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(
        probe,
        "urlopen",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(failure),
    )
    with caplog.at_level(logging.ERROR):
        result = probe.request_coverage(
            "secret",
            probe._CoverageRequest("000", "coverage", [], tmp_path / "sample", 20),
        )
    assert result == 1
    assert "secret" not in caplog.text


def test_request_coverage_rejects_bad_payload(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(
        probe,
        "urlopen",
        lambda *_args, **_kwargs: FakeResponse(
            b"<error/>",
            {"Content-Type": "application/xml"},
        ),
    )
    assert (
        probe.request_coverage(
            "secret",
            probe._CoverageRequest("000", "coverage", [], tmp_path / "sample", 20),
        )
        == 1
    )


def test_capabilities_summary_filters_and_handles_empty_titles(
    caplog: pytest.LogCaptureFixture,
) -> None:
    xml = b"""<Capabilities>
      <CoverageSummary><CoverageId>Z___2026-10-04</CoverageId>
        <CoverageTitle>Geopotential</CoverageTitle></CoverageSummary>
      <CoverageSummary><CoverageId>T___2026-10-04</CoverageId>
        <CoverageTitle>Temperature</CoverageTitle></CoverageSummary>
      <CoverageSummary><CoverageId>NO_TITLE</CoverageId></CoverageSummary>
    </Capabilities>"""
    with caplog.at_level(logging.INFO):
        probe._capabilities_summary(xml, "geo")
    assert "Coverage count: 3" in caplog.text
    assert "Geopotential" in caplog.text
    assert "Temperature" not in caplog.text


def test_axis_summary_reports_sparse_coefficients() -> None:
    axis = ET.fromstring(
        "<GeneralGridAxis><gridAxesSpanned>pressure</gridAxesSpanned>"
        "<coefficients>500 700</coefficients><offsetVector>0 0 1</offsetVector>"
        "</GeneralGridAxis>"
    )
    assert probe._axis_summary(axis) == (
        "axis=pressure coefficient_count=2 coefficients=['500', '700'] offset_vectors=['0 0 1']"
    )
    assert probe._axis_summary(ET.fromstring("<GeneralGridAxis />")) is None


def test_axis_summary_truncates_long_coefficients() -> None:
    coordinates = " ".join(str(number) for number in range(45))
    axis = ET.fromstring(
        f"<GeneralGridAxis><gridAxesSpanned>time</gridAxesSpanned>"
        f"<coefficients>{coordinates}</coefficients></GeneralGridAxis>"
    )
    summary = probe._axis_summary(axis)
    assert summary is not None
    assert "coefficient_count=45" in summary
    assert "'...'" in summary


def test_describe_summary_logs_selected_fields(caplog: pytest.LogCaptureFixture) -> None:
    body = b"""<DescribeCoverage>
      <CoverageId>run</CoverageId><lowerCorner>0 0</lowerCorner>
      <upperCorner>1 1</upperCorner><unknown>ignored</unknown>
      <EnvelopeWithTimePeriod xmlns:x="urn:x" x:axis="time" />
      <GeneralGridAxis><gridAxesSpanned>lat</gridAxesSpanned></GeneralGridAxis>
    </DescribeCoverage>"""
    with caplog.at_level(logging.INFO):
        probe._describe_summary(body)
    assert "CoverageId=run" in caplog.text
    assert "lowerCorner=0 0" in caplog.text
    assert "EnvelopeWithTimePeriod@axis=time" in caplog.text
    assert "axis=lat" in caplog.text
    assert "unknown" not in caplog.text


def test_request_metadata_handles_capabilities_and_description(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[str] = []

    def fake_request_xml(
        _token: str,
        _member: str,
        operation: str,
        _params: dict[str, str],
    ) -> bytes:
        calls.append(operation)
        return b"<Capabilities><CoverageSummary /></Capabilities>"

    monkeypatch.setattr(probe, "request_xml", fake_request_xml)
    assert probe.request_metadata("secret", member="000", coverage_id=None, title_filter=None) == 0
    assert (
        probe.request_metadata(
            "secret",
            member="000",
            coverage_id="coverage",
            title_filter=None,
        )
        == 0
    )
    assert calls == ["GetCapabilities", "DescribeCoverage"]


def test_request_metadata_propagates_failed_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(probe, "request_xml", lambda *_args, **_kwargs: None)
    assert probe.request_metadata("secret", member="000", coverage_id=None, title_filter=None) == 1


def test_main_checks_env_without_network(
    monkeypatch: pytest.MonkeyPatch,
    token_file: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(sys, "argv", ["probe", "--env-file", str(token_file), "--check-env-only"])
    with caplog.at_level(logging.INFO):
        assert probe.main() == 0
    assert str(token_file.resolve()) in caplog.text
    assert "secret-token" not in caplog.text


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "--get-coverage",
            "--coverage-id",
            "coverage",
            "--output",
            "sample.grib",
            "--max-bytes",
            "0",
        ],
        ["--subset", "time(0)"],
    ],
)
def test_main_rejects_invalid_argument_combinations(
    monkeypatch: pytest.MonkeyPatch,
    token_file: Path,
    arguments: list[str],
) -> None:
    monkeypatch.setattr(
        sys,
        "argv",
        ["probe", "--env-file", str(token_file), *arguments],
    )
    with pytest.raises(SystemExit):
        probe.main()


def test_main_routes_metadata_operation(
    monkeypatch: pytest.MonkeyPatch,
    token_file: Path,
) -> None:
    monkeypatch.setattr(sys, "argv", ["probe", "--env-file", str(token_file)])
    monkeypatch.setattr(
        probe,
        "request_metadata",
        lambda *_args, **_kwargs: 0,
    )
    assert probe.main() == 0
