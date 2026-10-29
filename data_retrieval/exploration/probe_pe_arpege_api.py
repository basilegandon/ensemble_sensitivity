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
from typing import TYPE_CHECKING, Protocol, Self, cast
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
REDACTED_VALUE = "[REDACTED]"


class _HttpResponse(Protocol):
    """Response interface needed by the bounded coverage reader."""

    headers: Message
    status: int

    def __enter__(self) -> Self:
        """Enter the response context."""

    def read(self, _amount: int = -1, /) -> bytes:
        """Read at most the requested number of bytes."""

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
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
        token = _token_from_line(raw_line)
        if token is not None:
            return token

    message = f"{VARIABLE_NAME} is missing or empty in {env_path}"
    raise ValueError(message)


def _token_from_line(raw_line: str) -> str | None:
    """Parse one dotenv line for the configured non-empty API token.

    Returns:
        Token value, or None when the line defines no token.

    """
    line = _normalize_token_assignment(raw_line)
    if line is None:
        return None
    key, separator, value = line.partition("=")
    if not separator or key.strip() != VARIABLE_NAME:
        return None
    return _non_empty_token(value)


def _normalize_token_assignment(raw_line: str) -> str | None:
    """Trim a dotenv line and remove an optional export prefix.

    Returns:
        Normalized assignment, or None for a blank or comment line.

    """
    line = raw_line.strip()
    if not line or line.startswith("#"):
        return None
    return line.removeprefix("export ").lstrip() if line.startswith("export ") else line


def _non_empty_token(value: str) -> str | None:
    """Remove optional token quotes and return only non-empty values.

    Returns:
        Unquoted token, or None when the value is empty.

    """
    token = value.strip().strip("\"'")
    return token or None


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
        response = cast("_HttpResponse", urlopen(request, timeout=30))
        with response:
            logger.info("HTTP status: %d", response.status)
            logger.info("Content-Type: %s", response.headers.get("Content-Type", "unknown"))
            return response.read()
    except HTTPError as error:
        error_body = error.read(2048).decode("utf-8", errors="replace")
        safe_body = error_body.replace(token, REDACTED_VALUE)
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
    if _declared_response_too_large(content_length, max_bytes):
        logger.error("Response Content-Length %s exceeds limit %d", content_length, max_bytes)
        return None
    return _read_bounded_grib(response, content_type, token, max_bytes)


def _declared_response_too_large(content_length: str | None, max_bytes: int) -> bool:
    """Check whether a non-empty declared response length exceeds the limit.

    Returns:
        Whether the response's declared size is above the configured limit.

    """
    return content_length is not None and bool(content_length) and int(content_length) > max_bytes


def _read_bounded_grib(
    response: _HttpResponse,
    content_type: str,
    token: str,
    max_bytes: int,
) -> bytes | None:
    """Read and validate a GRIB body against its size and content type.

    Returns:
        Accepted GRIB bytes, or None when the response is invalid.

    """
    body = response.read(max_bytes + 1)
    if len(body) > max_bytes:
        logger.error("Response exceeds limit of %d bytes", max_bytes)
        return None
    if "xml" in content_type.casefold():
        safe_body = body.decode("utf-8", errors="replace").replace(token, REDACTED_VALUE)
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
        response = cast("_HttpResponse", urlopen(request, timeout=60))
    except HTTPError as error:
        error_body = error.read(2048).decode("utf-8", errors="replace")
        safe_body = error_body.replace(token, REDACTED_VALUE)
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
    by_title = _coverage_ids_by_title(ET.fromstring(body))
    logger.info("Capabilities XML bytes: %d", len(body))
    logger.info("Coverage count: %d", sum(len(ids) for ids in by_title.values()))
    logger.info("Distinct coverage titles: %d", len(by_title))
    for title, coverage_ids in sorted(by_title.items()):
        _log_coverage_title(title, coverage_ids, title_filter)


def _coverage_ids_by_title(root: ET.Element) -> dict[str, list[str]]:
    """Group advertised coverage identifiers by their title.

    Returns:
        Coverage IDs grouped under their advertised title.

    """
    by_title: dict[str, list[str]] = defaultdict(list)
    for summary in root.iter():
        if _local_name(summary) != "CoverageSummary":
            continue
        fields = _coverage_summary_fields(summary)
        coverage_id = fields.get("CoverageId", "")
        if coverage_id:
            by_title[fields.get("CoverageTitle", "")].append(coverage_id)
    return by_title


def _coverage_summary_fields(summary: ET.Element) -> dict[str, str]:
    """Collect non-empty descendant text fields from a coverage summary.

    Returns:
        XML local names mapped to their non-empty text values.

    """
    fields: dict[str, str] = {}
    for child in summary.iter():
        if child is summary:
            continue
        value = (child.text or "").strip()
        if value:
            fields[_local_name(child)] = value
    return fields


def _title_matches(title: str, title_filter: str | None) -> bool:
    """Check whether a title passes an optional case-insensitive filter.

    Returns:
        Whether the title passes the optional filter.

    """
    return title_filter is None or not title_filter or title_filter.casefold() in title.casefold()


def _log_coverage_title(
    title: str,
    coverage_ids: list[str],
    title_filter: str | None,
) -> None:
    """Log the identifiers for one title when it passes the optional filter."""
    if not _title_matches(title, title_filter):
        return
    run_ids = sorted({coverage_id.split("___", 1)[-1] for coverage_id in coverage_ids})
    logger.info(
        "%s: %d coverages; %s .. %s",
        title,
        len(coverage_ids),
        run_ids[0],
        run_ids[-1],
    )
    for coverage_id in coverage_ids:
        logger.info("  %s", coverage_id)


def _axis_summary(element: ET.Element) -> str | None:
    """Summarize one WCS grid axis.

    Args:
        element: A `GeneralGridAxis` XML element.

    Returns:
        A concise axis description, or None when the axis name is absent.

    """
    axis, coefficients, vectors = _grid_axis_components(element)
    if axis is None:
        return None
    values = coefficients.split() if coefficients is not None and len(coefficients) > 0 else []
    sample = _sample_axis_coefficients(values)
    return (
        f"axis={axis} coefficient_count={len(values)} "
        f"coefficients={sample} offset_vectors={vectors}"
    )


def _grid_axis_components(
    element: ET.Element,
) -> tuple[str | None, str | None, list[str]]:
    """Read the axis name, coordinate coefficients, and offset vectors.

    Returns:
        Axis name, coefficients text, and non-empty offset vectors.

    """
    axis: str | None = None
    coefficients: str | None = None
    vectors: list[str] = []
    for child in element.iter():
        child_name = _local_name(child)
        value = (child.text or "").strip()
        axis, coefficients = _record_grid_axis_value(
            child_name,
            value,
            axis,
            coefficients,
            vectors,
        )
    return axis, coefficients, vectors


def _record_grid_axis_value(
    name: str,
    value: str,
    axis: str | None,
    coefficients: str | None,
    vectors: list[str],
) -> tuple[str | None, str | None]:
    """Accumulate one recognized grid-axis XML value.

    Returns:
        Updated axis name and coefficients.

    """
    if name == "gridAxesSpanned":
        return value, coefficients
    if name == "coefficients":
        return axis, value
    if name == "offsetVector" and value:
        vectors.append(value)
    return axis, coefficients


def _sample_axis_coefficients(values: list[str]) -> list[str]:
    """Keep short coordinate lists intact and truncate long ones for logging.

    Returns:
        The original list or a first/last sample with an ellipsis.

    """
    if len(values) <= MAX_VERBOSE_AXIS_COORDINATES:
        return values
    return [*values[:5], "...", *values[-5:]]


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
        _log_description_element(element, scalar_fields)


def _log_description_element(element: ET.Element, scalar_fields: set[str]) -> None:
    """Log scalar, envelope, or grid-axis details from one XML element."""
    name = _local_name(element)
    if name in scalar_fields:
        _log_scalar_description(name, element)
    elif name == "EnvelopeWithTimePeriod":
        _log_envelope_attributes(element)
    elif name == "GeneralGridAxis":
        _log_grid_axis(element)


def _log_scalar_description(name: str, element: ET.Element) -> None:
    """Log a non-empty scalar metadata element."""
    value = (element.text or "").strip()
    if value:
        logger.info("%s=%s", name, value)


def _log_envelope_attributes(element: ET.Element) -> None:
    """Log each attribute from a temporal envelope."""
    for key, value in element.attrib.items():
        logger.info("EnvelopeWithTimePeriod@%s=%s", key.rsplit("}", 1)[-1], value)


def _log_grid_axis(element: ET.Element) -> None:
    """Log a summarized grid axis when it has a name."""
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
    parser = _build_parser()
    args = parser.parse_args()
    env_path = args.env_file.resolve()
    token = _read_cli_token(parser, env_path)
    _log_credential(env_path, token)
    return _run_cli_operation(args, parser, token)


def _build_parser() -> argparse.ArgumentParser:
    """Create the command-line parser for the API probe.

    Returns:
        Parser configured with every supported probe option.

    """
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
    return parser


def _read_cli_token(parser: argparse.ArgumentParser, env_path: Path) -> str:
    """Read the token or report a command-line usage error.

    Returns:
        The configured non-empty token.

    """
    try:
        return read_token(env_path)
    except (OSError, ValueError) as error:
        parser.error(str(error))


def _log_credential(env_path: Path, token: str) -> None:
    """Log credential presence and a non-secret fingerprint."""
    fingerprint = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    logger.info("Environment file: %s", env_path)
    logger.info("Variable: %s", VARIABLE_NAME)
    logger.info("Credential present: yes (%d characters)", len(token))
    logger.info("Credential SHA-256 prefix: %s", fingerprint)


def _run_cli_operation(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
    token: str,
) -> int:
    """Dispatch the selected probe operation after shared argument checks.

    Returns:
        Exit status from the selected operation.

    """
    if args.check_env_only:
        return 0
    if args.get_coverage:
        return request_coverage(token, _coverage_request_from_args(args, parser))
    _validate_metadata_args(args, parser)
    return request_metadata(
        token,
        member=args.member,
        coverage_id=args.coverage_id,
        title_filter=args.title_filter,
    )


def _coverage_request_from_args(
    args: argparse.Namespace,
    parser: argparse.ArgumentParser,
) -> _CoverageRequest:
    """Validate and construct the bounded GetCoverage request options.

    Returns:
        Validated fields used to issue one GetCoverage request.

    """
    if args.coverage_id is None or args.output is None:
        parser.error("--get-coverage requires --coverage-id and --output")
    if args.max_bytes < 1:
        parser.error("--max-bytes must be positive")
    return _CoverageRequest(
        member=args.member,
        coverage_id=args.coverage_id,
        subsets=args.subset,
        output=args.output,
        max_bytes=args.max_bytes,
    )


def _validate_metadata_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> None:
    """Reject options that are valid only with GetCoverage."""
    if args.subset or args.output is not None:
        parser.error("--subset and --output are only valid with --get-coverage")


if __name__ == "__main__":
    sys.exit(main())
