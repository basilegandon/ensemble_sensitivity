# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Probe PE-ARPEGE WCS metadata without printing the credential."""

from __future__ import annotations

import argparse
import hashlib
import logging
import sys
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

if TYPE_CHECKING:
    from email.message import Message
    from types import TracebackType

logger = logging.getLogger(__name__)
VARIABLE_NAME = "PEARP_METEO_FRANCE_API_TOKEN"
API_BASE_URL = "https://public-api.meteofrance.fr/public/pearpege/1.0"
MAX_VERBOSE_AXIS_COORDINATES = 40


class _HttpResponse(Protocol):
    """Response interface needed by the bounded coverage reader."""

    headers: Message
    status: int

    def __enter__(self) -> Self:
        """Enter the response context."""

    def read(self, amount: int = -1, /) -> bytes:
        """Read at most the requested number of bytes."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the response context."""


@dataclass(frozen=True, slots=True)
class _CoverageRequest:
    """Bounded WCS GetCoverage request options."""

    member: str
    coverage_id: str
    subsets: list[str]
    output: Path
    max_bytes: int


def _local_name(element: ET.Element) -> str:
    """Return an XML element's local name without its namespace.

    Args:
        element: XML element.

    Returns:
        The local tag name.

    """
    return element.tag.rsplit("}", 1)[-1]


def read_token(env_path: Path) -> str:
    """Read the configured API token from a dotenv-style file.

    Args:
        env_path: Path to the local environment file.

    Returns:
        The non-empty API token.

    Raises:
        ValueError: If the variable is missing or empty.

    """
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line.removeprefix("export ").lstrip()
        key, separator, value = line.partition("=")
        if separator and key.strip() == VARIABLE_NAME:
            token = value.strip().strip("\"'")
            if token:
                return token
            break

    message = f"{VARIABLE_NAME} is missing or empty in {env_path}"
    raise ValueError(message)


def request_xml(token: str, member: str, operation: str, params: dict[str, str]) -> bytes | None:
    """Issue one authenticated WCS metadata request.

    Args:
        token: API key to send in the `apikey` header.
        member: Three-digit ensemble member endpoint.
        operation: WCS operation name.
        params: Query parameters for the WCS operation.

    Returns:
        Response bytes, or None after an HTTP/network failure.

    """
    path = f"/wcs/MF-NWP-GLOBAL-PEARP{member}-025-GLOBE-WCS/{operation}"
    query = urlencode(params)
    request = Request(
        f"{API_BASE_URL}{path}?{query}",
        headers={
            "apikey": token,
            "Accept": "application/xml",
            "Cache-Control": "no-cache",
        },
    )
    try:
        response: _HttpResponse = urlopen(request, timeout=30)
        with response:
            logger.info("HTTP status: %d", response.status)
            logger.info("Content-Type: %s", response.headers.get("Content-Type", "unknown"))
            return response.read()
    except HTTPError as error:
        error_body = error.read(2048).decode("utf-8", errors="replace")
        safe_body = error_body.replace(token, "[REDACTED]")
        logger.exception(
            "HTTP status %d; Content-Type: %s; response prefix: %r",
            error.code,
            error.headers.get("Content-Type", "unknown"),
            safe_body[:800],
        )
        return None
    except URLError:
        logger.exception("Unable to reach the PE-ARPEGE API")
        return None


def _read_coverage_response(
    response: _HttpResponse,
    token: str,
    max_bytes: int,
) -> bytes | None:
    """Read a bounded GRIB payload and reject service errors.

    Args:
        response: Open WCS response.
        token: API key to redact from unexpected response text.
        max_bytes: Maximum response size accepted.

    Returns:
        GRIB bytes, or None when the response is too large or not GRIB.

    """
    content_type = response.headers.get("Content-Type", "unknown")
    content_length = response.headers.get("Content-Length")
    logger.info("HTTP status: %d", response.status)
    logger.info("Content-Type: %s", content_type)
    if content_length and int(content_length) > max_bytes:
        logger.error("Response Content-Length %s exceeds limit %d", content_length, max_bytes)
        return None
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        logger.error("Response exceeds limit of %d bytes", max_bytes)
        return None
    if "xml" in content_type.casefold():
        safe_body = body.decode("utf-8", errors="replace").replace(token, "[REDACTED]")
        logger.error("Expected GRIB but received XML: %r", safe_body[:800])
        return None
    return body


def request_coverage(token: str, options: _CoverageRequest) -> int:
    """Fetch one bounded WCS GRIB response to a local file.

    Args:
        token: API key read from the local environment file.
        options: Member, coverage, subset, output, and size settings.

    Returns:
        Zero on success; one when the request fails or exceeds the size limit.

    """
    path = f"/wcs/MF-NWP-GLOBAL-PEARP{options.member}-025-GLOBE-WCS/GetCoverage"
    params = [
        ("service", "WCS"),
        ("version", "2.0.1"),
        ("coverageid", options.coverage_id),
        ("format", "application/wmo-grib"),
        *(("subset", subset) for subset in options.subsets),
    ]
    request = Request(
        f"{API_BASE_URL}{path}?{urlencode(params)}",
        headers={"apikey": token, "Accept": "application/wmo-grib", "Cache-Control": "no-cache"},
    )
    try:
        response: _HttpResponse = urlopen(request, timeout=60)
    except HTTPError as error:
        error_body = error.read(2048).decode("utf-8", errors="replace")
        safe_body = error_body.replace(token, "[REDACTED]")
        logger.exception("WCS GetCoverage failed: %d; %r", error.code, safe_body[:800])
        return 1
    except URLError:
        logger.exception("Unable to reach the PE-ARPEGE API")
        return 1

    with response:
        body = _read_coverage_response(response, token, options.max_bytes)
    if body is None:
        return 1
    options.output.parent.mkdir(parents=True, exist_ok=True)
    options.output.write_bytes(body)
    logger.info("Saved %d response bytes to %s", len(body), options.output.resolve())
    return 0


def _capabilities_summary(body: bytes, title_filter: str | None) -> None:
    """Log a compact inventory of available WCS coverages.

    Args:
        body: Capabilities response bytes.
        title_filter: Optional case-insensitive coverage-title filter.

    """
    root = ET.fromstring(body)
    by_title: dict[str, list[str]] = defaultdict(list)
    for summary in root.iter():
        if _local_name(summary) != "CoverageSummary":
            continue
        fields = {
            _local_name(child): (child.text or "").strip()
            for child in summary.iter()
            if child is not summary and (child.text or "").strip()
        }
        coverage_id = fields.get("CoverageId", "")
        title = fields.get("CoverageTitle", "")
        if coverage_id:
            by_title[title].append(coverage_id)

    logger.info("Capabilities XML bytes: %d", len(body))
    logger.info("Coverage count: %d", sum(len(ids) for ids in by_title.values()))
    logger.info("Distinct coverage titles: %d", len(by_title))
    for title, coverage_ids in sorted(by_title.items()):
        if title_filter and title_filter.casefold() not in title.casefold():
            continue
        run_ids = sorted({coverage_id.split("___", 1)[-1] for coverage_id in coverage_ids})
        logger.info("%s: %d coverages; %s .. %s", title, len(coverage_ids), run_ids[0], run_ids[-1])
        for coverage_id in coverage_ids:
            logger.info("  %s", coverage_id)


def _axis_summary(element: ET.Element) -> str | None:
    """Summarize one WCS grid axis.

    Args:
        element: A `GeneralGridAxis` XML element.

    Returns:
        A concise axis description, or None when the axis name is absent.

    """
    axis: str | None = None
    coefficients: str | None = None
    vectors: list[str] = []
    for child in element.iter():
        child_name = _local_name(child)
        value = (child.text or "").strip()
        if child_name == "gridAxesSpanned":
            axis = value
        elif child_name == "coefficients":
            coefficients = value
        elif child_name == "offsetVector" and value:
            vectors.append(value)
    if axis is None:
        return None
    values = coefficients.split() if coefficients else []
    sample = (
        values
        if len(values) <= MAX_VERBOSE_AXIS_COORDINATES
        else [*values[:5], "...", *values[-5:]]
    )
    return (
        f"axis={axis} coefficient_count={len(values)} "
        f"coefficients={sample} offset_vectors={vectors}"
    )


def _describe_summary(body: bytes) -> None:
    """Log spatial, temporal, and range-axis metadata for one coverage.

    Args:
        body: DescribeCoverage response bytes.

    """
    root = ET.fromstring(body)
    logger.info("DescribeCoverage XML bytes: %d", len(body))
    scalar_fields = {
        "CoverageId",
        "CoverageTitle",
        "axisLabels",
        "srsName",
        "lowerCorner",
        "upperCorner",
        "beginPosition",
        "endPosition",
        "low",
        "high",
    }
    for element in root.iter():
        name = _local_name(element)
        if name in scalar_fields:
            value = (element.text or "").strip()
            if value:
                logger.info("%s=%s", name, value)
        elif name == "EnvelopeWithTimePeriod":
            for key, value in element.attrib.items():
                logger.info("EnvelopeWithTimePeriod@%s=%s", key.rsplit("}", 1)[-1], value)
        elif name == "GeneralGridAxis":
            summary = _axis_summary(element)
            if summary is not None:
                logger.info("%s", summary)


def request_metadata(
    token: str,
    *,
    member: str,
    coverage_id: str | None,
    title_filter: str | None,
) -> int:
    """Fetch capabilities or a selected coverage description.

    Args:
        token: API key read from the local environment file.
        member: Three-digit ensemble member endpoint.
        coverage_id: Coverage to describe, or None to list capabilities.
        title_filter: Optional filter for coverage titles.

    Returns:
        Zero on success; one when the request fails.

    """
    if coverage_id is None:
        body = request_xml(
            token,
            member,
            "GetCapabilities",
            {"service": "WCS", "version": "2.0.1", "language": "eng"},
        )
        if body is None:
            return 1
        _capabilities_summary(body, title_filter)
        return 0

    body = request_xml(
        token,
        member,
        "DescribeCoverage",
        {"service": "WCS", "version": "2.0.1", "coverageid": coverage_id},
    )
    if body is None:
        return 1
    _describe_summary(body)
    return 0


def main() -> int:
    """Read the local credential and optionally test WCS access.

    Returns:
        Process exit code.

    """
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--env-file",
        type=Path,
        default=Path(".env"),
        help="dotenv-style file containing the PE-ARPEGE token (default: .env)",
    )
    parser.add_argument(
        "--member",
        choices=[f"{number:03}" for number in range(35)],
        default="000",
        help="PE-ARPEGE ensemble-member endpoint (default: 000)",
    )
    parser.add_argument(
        "--coverage-id",
        help="describe or fetch this exact coverage instead of listing capabilities",
    )
    parser.add_argument(
        "--get-coverage",
        action="store_true",
        help="fetch a bounded GRIB response; requires --coverage-id and --output",
    )
    parser.add_argument(
        "--subset",
        action="append",
        default=[],
        help="repeat for each WCS axis expression, e.g. --subset 'pressure(500)'",
    )
    parser.add_argument(
        "--output",
        type=Path,
        help="destination file for --get-coverage",
    )
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=16 * 1024 * 1024,
        help="maximum response size accepted for --get-coverage",
    )
    parser.add_argument(
        "--title-filter",
        help="only print capability IDs whose coverage title contains this text",
    )
    parser.add_argument(
        "--check-env-only",
        action="store_true",
        help="verify the file and report a non-secret fingerprint without an API request",
    )
    args = parser.parse_args()
    env_path = args.env_file.resolve()

    try:
        token = read_token(env_path)
    except (OSError, ValueError) as error:
        parser.error(str(error))

    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    logger.info("Environment file: %s", env_path)
    logger.info("Variable: %s", VARIABLE_NAME)
    logger.info("Credential present: yes (%d characters)", len(token))
    logger.info("Credential SHA-256 prefix: %s", fingerprint)

    if args.check_env_only:
        return 0
    if args.get_coverage:
        if args.coverage_id is None or args.output is None:
            parser.error("--get-coverage requires --coverage-id and --output")
        if args.max_bytes < 1:
            parser.error("--max-bytes must be positive")
        return request_coverage(
            token,
            _CoverageRequest(
                member=args.member,
                coverage_id=args.coverage_id,
                subsets=args.subset,
                output=args.output,
                max_bytes=args.max_bytes,
            ),
        )
    if args.subset or args.output is not None:
        parser.error("--subset and --output are only valid with --get-coverage")

    return request_metadata(
        token,
        member=args.member,
        coverage_id=args.coverage_id,
        title_filter=args.title_filter,
    )


if __name__ == "__main__":
    sys.exit(main())
