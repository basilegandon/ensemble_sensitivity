# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Measure a complete, sequential WCS Z500 retrieval for one analysis target."""

from __future__ import annotations

import argparse
import importlib
import json
import logging
import re
import sys
import tempfile
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen
from xml.etree import ElementTree as ET

from docs.probe_pe_arpege_api import API_BASE_URL, VARIABLE_NAME, read_token

if TYPE_CHECKING:
    from email.message import Message
    from types import TracebackType

logger = logging.getLogger(__name__)
CAPABILITIES_PATH = "/wcs/MF-NWP-GLOBAL-PEARP000-025-GLOBE-WCS/GetCapabilities"
COVERAGE_PREFIX = "GEOPOTENTIAL__ISOBARIC_SURFACE___"
MEMBERS = range(35)
MAX_FORECAST_LEAD_HOURS = 102
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
GRIB_MIN_MESSAGE_BYTES = 20
HTTP_TIMEOUT_SECONDS = 90
HTTP_TOO_MANY_REQUESTS = 429
MAX_THROTTLE_RETRIES = 5
MAX_NETWORK_RETRIES = 2
THROTTLE_PATTERN = re.compile(r'"nextAccessTime"\s*:\s*"([^"]+)"')


class PilotError(Exception):
    """Raised when the WCS pilot cannot retrieve a valid requested field."""


class _HttpResponse(Protocol):
    """HTTP response interface used by the pilot."""

    headers: Message
    status: int

    def __enter__(self) -> Self:
        """Enter the response context."""

    def read(self, amount: int = -1, /) -> bytes:
        """Read response bytes."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Close the response context."""


class _Eccodes(Protocol):
    """Small typed surface of the optional ecCodes Python binding."""

    def codes_get(self, handle: object, key: str) -> int | float | str:
        """Read a GRIB metadata key."""

    def codes_new_from_message(self, message: bytes) -> object:
        """Create a GRIB handle from a message."""

    def codes_release(self, handle: object) -> None:
        """Release a GRIB handle."""


@dataclass(frozen=True, slots=True)
class PilotOptions:
    """Inputs for one complete sequential WCS pilot."""

    target_lead_hours: int
    step_hours: int
    report_path: Path
    env_path: Path


@dataclass(frozen=True, slots=True)
class FieldResult:
    """Validated result for one member and one forecast lead."""

    member: int
    lead_hours: int
    http_status: int
    content_type: str
    bytes_received: int
    elapsed_seconds: float
    throttle_retries: int
    throttle_wait_seconds: float
    network_retries: int
    grib_metadata: dict[str, int | float | str]


@dataclass(frozen=True, slots=True)
class FieldRequest:
    """Identity and expected initialization for one field request."""

    coverage_id: str
    member: int
    lead_hours: int
    initialization: datetime


@dataclass(frozen=True, slots=True)
class RequestMetrics:
    """Timing and throttle history for one API request."""

    started: float
    throttle_retries: int
    throttle_wait_seconds: float
    network_retries: int


def required_leads(target_lead_hours: int, step_hours: int) -> list[int]:
    """Return the target and preceding leads at the requested analysis interval.

    Returns:
        Forecast leads in descending order, including zero when reached.

    Raises:
        ValueError: If the target lead is negative or the step is not positive.

    """
    if target_lead_hours < 0:
        raise ValueError("negative target lead")
    if target_lead_hours > MAX_FORECAST_LEAD_HOURS:
        raise ValueError("target lead exceeds API maximum")
    if step_hours < 1:
        raise ValueError("non-positive time step")
    return list(range(target_lead_hours, -1, -step_hours))


def _local_name(element: ET.Element) -> str:
    """Return an XML element name without its namespace.

    Returns:
        Local tag name.

    """
    return element.tag.rsplit("}", 1)[-1]


def latest_coverage_id(capabilities: bytes) -> str:
    """Select the newest advertised isobaric-geopotential coverage.

    Args:
        capabilities: WCS capabilities XML response.

    Returns:
        Coverage identifier for the newest initialization.

    Raises:
        PilotError: If no coverage is listed or an initialization is malformed.

    """
    root = ET.fromstring(capabilities)
    coverage_ids = [
        (element.text or "").strip()
        for element in root.iter()
        if _local_name(element) == "CoverageId"
        and (element.text or "").strip().startswith(COVERAGE_PREFIX)
    ]
    if not coverage_ids:
        raise PilotError("No isobaric geopotential coverage was advertised")

    def initialization(coverage_id: str) -> datetime:
        timestamp = coverage_id.removeprefix(COVERAGE_PREFIX)
        try:
            return datetime.strptime(timestamp, "%Y-%m-%dT%H.%M.%SZ").replace(tzinfo=UTC)
        except ValueError as error:
            message = f"Invalid initialization timestamp in {coverage_id!r}"
            raise PilotError(message) from error

    return max(coverage_ids, key=initialization)


def next_access_time(error_body: str) -> datetime | None:
    """Parse the service's optional retry timestamp from a throttling response.

    Args:
        error_body: Bounded text returned by the API.

    Returns:
        UTC timestamp when the API says access may resume, or None if absent or
        malformed.

    """
    match = THROTTLE_PATTERN.search(error_body)
    if match is None:
        return None
    value = match.group(1)
    iso_value = value.replace(" UTC", "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(iso_value)
    except ValueError:
        normalized = re.sub(r"([A-Za-z]{3})\.", r"\1", value.replace(" UTC", ""))
        try:
            return datetime.strptime(normalized, "%Y-%b-%d %H:%M:%S%z")
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _load_eccodes() -> _Eccodes:
    """Load ecCodes lazily so metadata discovery does not need the binding.

    Returns:
        Typed interface to the ecCodes Python binding.

    Raises:
        PilotError: If the optional ecCodes binding is unavailable.

    """
    try:
        module = importlib.import_module("eccodes")
    except ImportError as error:
        raise PilotError(
            "The WCS pilot requires ecCodes; run it with "
            "`uv run --no-project --with eccodes python -m docs.pilot_pearp_wcs`."
        ) from error

    return cast("_Eccodes", module)


def _decode_single_message(body: bytes, eccodes: _Eccodes) -> dict[str, int | float | str]:
    """Decode and validate one complete global GRIB field.

    Args:
        body: WCS response bytes.
        eccodes: ecCodes functions used to inspect the message.

    Returns:
        Validated-key values from the GRIB message.

    Raises:
        PilotError: If the response is not exactly one complete GRIB message.

    """
    if len(body) < GRIB_MIN_MESSAGE_BYTES or body[:4] != b"GRIB" or body[-4:] != b"7777":
        raise PilotError("WCS response is not one complete GRIB message")
    message_length = int.from_bytes(body[8:16], "big")
    if message_length != len(body):
        message = f"WCS returned {len(body)} bytes but GRIB header declares {message_length}"
        raise PilotError(message)
    handle = eccodes.codes_new_from_message(body)
    keys = (
        "paramId",
        "typeOfLevel",
        "level",
        "dataDate",
        "dataTime",
        "step",
        "number",
        "perturbationNumber",
        "numberOfForecastsInEnsemble",
        "gridType",
        "Ni",
        "Nj",
        "units",
    )
    try:
        return {key: eccodes.codes_get(handle, key) for key in keys}
    finally:
        eccodes.codes_release(handle)


def validate_metadata(
    metadata: dict[str, int | float | str],
    *,
    member: int,
    lead_hours: int,
    initialization: datetime,
) -> None:
    """Reject a response that is not the requested complete-grid Z500 field.

    Args:
        metadata: Decoded GRIB metadata.
        member: Expected ensemble member.
        lead_hours: Expected forecast lead.
        initialization: Expected model initialization.

    Raises:
        PilotError: If any required GRIB metadata differs from expectations.

    """
    expected: dict[str, int | str] = {
        "paramId": 129,
        "typeOfLevel": "isobaricInhPa",
        "level": 500,
        "dataDate": int(initialization.strftime("%Y%m%d")),
        "dataTime": initialization.hour * 100,
        "step": lead_hours,
        "number": member,
        "perturbationNumber": member,
        "numberOfForecastsInEnsemble": 35,
        "gridType": "regular_ll",
        "Ni": 1440,
        "Nj": 721,
        "units": "m**2 s**-2",
    }
    mismatches = {
        key: (metadata.get(key), value)
        for key, value in expected.items()
        if metadata.get(key) != value
    }
    if mismatches:
        message = f"GRIB metadata mismatch for member {member}, lead {lead_hours}: {mismatches}"
        raise PilotError(message)


def _get_capabilities(token: str) -> bytes:
    """Fetch global GLOB025 capabilities for member endpoint 000.

    Args:
        token: Météo-France API key.

    Returns:
        Raw capabilities XML.

    Raises:
        PilotError: If the API response is not XML.

    """
    query = urlencode({"service": "WCS", "version": "2.0.1", "language": "eng"})
    request = Request(
        f"{API_BASE_URL}{CAPABILITIES_PATH}?{query}",
        headers={"apikey": token, "Accept": "application/xml", "Cache-Control": "no-cache"},
    )
    response: _HttpResponse = urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
    with response:
        body = response.read()
    if b"<" not in body[:100]:
        raise PilotError("GetCapabilities response is not XML")
    return body


def _open_with_retries(
    request: Request,
    member: int,
    lead_hours: int,
) -> tuple[_HttpResponse, int, float, int]:
    """Open one request with bounded network and server-directed quota retries.

    Args:
        request: Authenticated WCS request.
        member: Member ID for diagnostics.
        lead_hours: Forecast lead for diagnostics.

    Returns:
        Open response, number of quota retries, quota wait seconds, and network
        retries.

    Raises:
        PilotError: If the request fails after the retry limits.

    """
    throttle_retries = 0
    throttle_wait_seconds = 0.0
    network_retries = 0
    while True:
        try:
            response: _HttpResponse = urlopen(request, timeout=HTTP_TIMEOUT_SECONDS)
        except HTTPError as error:
            error_body = error.read(4096).decode("utf-8", errors="replace")
            retry_time = (
                next_access_time(error_body) if error.code == HTTP_TOO_MANY_REQUESTS else None
            )
            if retry_time is None or throttle_retries >= MAX_THROTTLE_RETRIES:
                suffix = f"; nextAccessTime={retry_time.isoformat()}" if retry_time else ""
                raise PilotError(
                    f"HTTP {error.code} for member {member:03}, lead {lead_hours} h{suffix}"
                ) from error
            wait_seconds = max(0.0, (retry_time - datetime.now(UTC)).total_seconds()) + 1.0
            throttle_retries += 1
            throttle_wait_seconds += wait_seconds
            logger.warning(
                "HTTP 429; waiting %.1f s until %s before retrying member %03d lead %d h",
                wait_seconds,
                retry_time.isoformat(),
                member,
                lead_hours,
            )
            time.sleep(wait_seconds)
        except TimeoutError as error:
            if network_retries >= MAX_NETWORK_RETRIES:
                raise PilotError(
                    f"Timed out for member {member:03}, lead {lead_hours} h "
                    f"after {network_retries} retries"
                ) from error
            network_retries += 1
            retry_delay = float(network_retries)
            logger.warning(
                "Timeout; retrying member %03d lead %d h after %.1f s (%d/%d)",
                member,
                lead_hours,
                retry_delay,
                network_retries,
                MAX_NETWORK_RETRIES,
            )
            time.sleep(retry_delay)
        except URLError as error:
            raise PilotError(
                f"Network failure for member {member:03}, lead {lead_hours} h: {error.reason}"
            ) from error
        except OSError as error:
            if network_retries >= MAX_NETWORK_RETRIES:
                raise PilotError(
                    f"Network failure for member {member:03}, lead {lead_hours} h "
                    f"after {network_retries} retries: {error}"
                ) from error
            network_retries += 1
            retry_delay = float(network_retries)
            logger.warning(
                "Network error; retrying member %03d lead %d h after %.1f s (%d/%d): %s",
                member,
                lead_hours,
                retry_delay,
                network_retries,
                MAX_NETWORK_RETRIES,
                error,
            )
            time.sleep(retry_delay)
        else:
            return response, throttle_retries, throttle_wait_seconds, network_retries


def _validate_response(
    response: _HttpResponse,
    field_request: FieldRequest,
    eccodes: _Eccodes,
    request_metrics: RequestMetrics,
) -> FieldResult:
    """Validate one WCS response and capture its transfer metrics.

    Args:
        response: Open WCS response.
        field_request: Expected coverage, member, lead, and initialization.
        eccodes: ecCodes functions used to validate the response.
        request_metrics: Request timing and quota retry details.

    Returns:
        Validated response metrics and GRIB metadata.

    Raises:
        PilotError: If the response is oversized or not the requested GRIB field.

    """
    member = field_request.member
    lead_hours = field_request.lead_hours
    content_type = response.headers.get("Content-Type", "unknown")
    content_length = response.headers.get("Content-Length")
    if content_length and int(content_length) > MAX_RESPONSE_BYTES:
        message = (
            f"Response Content-Length {content_length} exceeds "
            f"{MAX_RESPONSE_BYTES} bytes for member {member:03}, lead {lead_hours} h"
        )
        raise PilotError(message)
    body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        message = f"Response exceeds {MAX_RESPONSE_BYTES} bytes for member {member:03}, "
        raise PilotError(f"{message}lead {lead_hours} h")
    if "xml" in content_type.casefold():
        message = f"Expected GRIB but received {content_type} for member {member:03}, "
        raise PilotError(f"{message}lead {lead_hours} h")
    metadata = _decode_single_message(body, eccodes)
    validate_metadata(
        metadata,
        member=member,
        lead_hours=lead_hours,
        initialization=field_request.initialization,
    )
    return FieldResult(
        member=member,
        lead_hours=lead_hours,
        http_status=response.status,
        content_type=content_type,
        bytes_received=len(body),
        elapsed_seconds=time.monotonic() - request_metrics.started,
        throttle_retries=request_metrics.throttle_retries,
        throttle_wait_seconds=request_metrics.throttle_wait_seconds,
        network_retries=request_metrics.network_retries,
        grib_metadata=metadata,
    )


def _request_field(
    token: str,
    field_request: FieldRequest,
    eccodes: _Eccodes,
) -> FieldResult:
    """Retrieve and validate one full-grid member/lead without saving it.

    Args:
        token: Météo-France API key.
        field_request: Coverage, member, lead, and initialization to retrieve.
        eccodes: ecCodes functions used to validate the response.

    Returns:
        Validated response metrics and GRIB metadata.

    """
    member = field_request.member
    lead_hours = field_request.lead_hours
    endpoint = f"/wcs/MF-NWP-GLOBAL-PEARP{member:03}-025-GLOBE-WCS/GetCoverage"
    parameters = urlencode(
        [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", field_request.coverage_id),
            ("format", "application/wmo-grib"),
            ("subset", "pressure(500)"),
            ("subset", f"time({lead_hours * 3600})"),
        ]
    )
    request = Request(
        f"{API_BASE_URL}{endpoint}?{parameters}",
        headers={
            "apikey": token,
            "Accept": "application/wmo-grib",
            "Cache-Control": "no-cache",
        },
    )
    started = time.monotonic()
    response, throttle_retries, throttle_wait_seconds, network_retries = _open_with_retries(
        request,
        member,
        lead_hours,
    )

    with response:
        return _validate_response(
            response,
            field_request,
            eccodes,
            RequestMetrics(
                started=started,
                throttle_retries=throttle_retries,
                throttle_wait_seconds=throttle_wait_seconds,
                network_retries=network_retries,
            ),
        )


def run_pilot(options: PilotOptions) -> int:
    """Run the sequential complete-member pilot and persist its measurement.

    Args:
        options: Target lead, step, and local paths for token and report.

    Returns:
        Zero when all requested fields pass validation, otherwise one.

    """
    leads = required_leads(options.target_lead_hours, options.step_hours)
    token = read_token(options.env_path)
    logger.info("Environment file: %s", options.env_path.resolve())
    logger.info("Credential variable: %s (present)", VARIABLE_NAME)
    capabilities = _get_capabilities(token)
    coverage_id = latest_coverage_id(capabilities)
    initialization = datetime.strptime(
        coverage_id.removeprefix(COVERAGE_PREFIX),
        "%Y-%m-%dT%H.%M.%SZ",
    ).replace(tzinfo=UTC)
    eccodes = _load_eccodes()
    results: list[FieldResult] = []
    started = time.monotonic()
    failure: str | None = None
    for lead in leads:
        for member in MEMBERS:
            try:
                result = _request_field(
                    token,
                    FieldRequest(
                        coverage_id=coverage_id,
                        member=member,
                        lead_hours=lead,
                        initialization=initialization,
                    ),
                    eccodes,
                )
            except PilotError as error:
                failure = str(error)
                logger.error("Pilot stopped: %s", failure)
                break
            results.append(result)
            logger.info(
                "member=%03d lead=%03dh status=%d bytes=%d elapsed=%.2fs",
                result.member,
                result.lead_hours,
                result.http_status,
                result.bytes_received,
                result.elapsed_seconds,
            )
        if failure:
            break

    complete = len(results) == len(leads) * len(MEMBERS)
    report = {
        "coverage_id": coverage_id,
        "initialization_utc": initialization.isoformat(),
        "target_lead_hours": options.target_lead_hours,
        "time_step_hours": options.step_hours,
        "required_leads_hours": leads,
        "members": len(MEMBERS),
        "requests_expected": len(leads) * len(MEMBERS),
        "requests_completed": len(results),
        "complete": complete,
        "failure": failure,
        "bytes_received": sum(result.bytes_received for result in results),
        "throttle_retries": sum(result.throttle_retries for result in results),
        "throttle_wait_seconds": round(
            sum(result.throttle_wait_seconds for result in results),
            3,
        ),
        "network_retries": sum(result.network_retries for result in results),
        "elapsed_seconds": round(time.monotonic() - started, 3),
        "mean_request_seconds": (
            round(sum(result.elapsed_seconds for result in results) / len(results), 3)
            if results
            else None
        ),
        "mean_response_bytes": (
            round(sum(result.bytes_received for result in results) / len(results))
            if results
            else None
        ),
        "results": [asdict(result) for result in results],
    }
    options.report_path.parent.mkdir(parents=True, exist_ok=True)
    options.report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    logger.info(
        "Pilot %s: %d/%d requests; %d bytes; %.1f seconds; report=%s",
        "complete" if complete else "incomplete",
        len(results),
        len(leads) * len(MEMBERS),
        report["bytes_received"],
        report["elapsed_seconds"],
        options.report_path.resolve(),
    )
    return 0 if complete else 1


def main() -> int:
    """Parse pilot options and run the API batch.

    Returns:
        Process exit status.

    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-lead-hours", type=int, default=24)
    parser.add_argument("--step-hours", type=int, default=24)
    parser.add_argument("--env-file", type=Path, default=Path(".env"))
    parser.add_argument(
        "--report",
        type=Path,
        help="JSON result report (default: a named file in the system temp directory)",
    )
    args = parser.parse_args()
    try:
        leads = required_leads(args.target_lead_hours, args.step_hours)
    except ValueError as error:
        parser.error(str(error))
    report_path = args.report
    if report_path is None:
        report_path = Path(tempfile.gettempdir()) / (
            f"pearp-wcs-pilot-latest-t{args.target_lead_hours}-s{args.step_hours}.json"
        )
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    logger.info(
        "Target lead: %d h; step: %d h; required leads: %s",
        args.target_lead_hours,
        args.step_hours,
        leads,
    )
    logger.info("Expected full batch size: %d sequential requests", len(leads) * len(MEMBERS))
    try:
        return run_pilot(
            PilotOptions(
                target_lead_hours=args.target_lead_hours,
                step_hours=args.step_hours,
                report_path=report_path,
                env_path=args.env_file,
            )
        )
    except (OSError, PilotError, ValueError) as error:
        logger.error("Unable to run WCS pilot: %s", error)
        return 1


if __name__ == "__main__":
    sys.exit(main())
