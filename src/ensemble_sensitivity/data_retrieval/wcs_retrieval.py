# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Retrieve and validate complete PEARP Z500 runs from the Météo-France WCS API."""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import shutil
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from itertools import product
from operator import itemgetter
from threading import Lock
from time import monotonic, sleep
from typing import TYPE_CHECKING, Protocol, Self, cast
from urllib.error import HTTPError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from defusedxml import ElementTree
from tqdm import tqdm

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping
    from email.message import Message
    from pathlib import Path
    from types import TracebackType

logger = logging.getLogger(__name__)

API_BASE_URL = "https://public-api.meteofrance.fr/public/pearpege/1.0"
CAPABILITIES_PATH = "/wcs/MF-NWP-GLOBAL-PEARP000-025-GLOBE-WCS/GetCapabilities"
COVERAGE_PREFIX = "GEOPOTENTIAL__ISOBARIC_SURFACE___"
VARIABLE_NAME = "PEARP_METEO_FRANCE_API_TOKEN"
MEMBERS = tuple(range(35))
MAX_FORECAST_LEAD_HOURS = 102
# DescribeCoverage advertises Z500 only every 3 h, from +0 h to +102 h.
LEAD_CADENCE_HOURS = 3
RAW_FETCH_DIRNAME = "raw_fetch"
MAX_RESPONSE_BYTES = 16 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 90
MAX_THROTTLE_RETRIES = 5
MAX_NETWORK_RETRIES = 2
API_REQUEST_LIMIT_PER_MINUTE = 400
REQUEST_RATE_MARGIN = 10
MAX_REQUESTS_PER_MINUTE = API_REQUEST_LIMIT_PER_MINUTE - REQUEST_RATE_MARGIN
REQUEST_WINDOW_SECONDS = 60.0
MIN_REQUEST_INTERVAL_SECONDS = REQUEST_WINDOW_SECONDS / (MAX_REQUESTS_PER_MINUTE - 1)
SCHEDULING_EPSILON_SECONDS = 0.001
GRIB_MIN_MESSAGE_BYTES = 20
HTTP_TOO_MANY_REQUESTS = 429
_THROTTLE_PATTERN = re.compile(r'"nextAccessTime"\s*:\s*"([^"]+)"')
_request_timestamps: deque[float] = deque()
_request_lock = Lock()


class RetrievalError(Exception):
    """Raised when required PEARP fields cannot be retrieved and validated."""


class _HttpResponse(Protocol):
    """Small typed surface of a WCS response."""

    headers: Message
    status: int

    def __enter__(self) -> Self:
        """Enter the response context."""

    def read(self, _amount: int = -1, /) -> bytes:
        """Read response bytes."""

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        """Close the response."""


class _Eccodes(Protocol):
    """ecCodes functions required to validate WCS responses."""

    def codes_get(self, handle: object, key: str) -> int | float | str:
        """Read a scalar GRIB key."""

    def codes_new_from_message(self, message: bytes) -> object:
        """Create a GRIB handle from a message."""

    def codes_release(self, handle: object) -> None:
        """Release a GRIB handle."""


class _XmlElement(Protocol):
    """Minimum typed XML element interface used for coverage discovery."""

    tag: str
    text: str | None

    def iter(self) -> Iterator[_XmlElement]:
        """Yield this element and its descendants."""


@dataclass(frozen=True, slots=True)
class RetrievalOptions:
    """Options for one complete latest-run retrieval."""

    target_lead_hours: int
    step_hours: int
    output_dir: Path
    env_file: Path
    coverage_id: str | None = None
    show_progress: bool = True
    executed_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """Traceability record for one saved member/lead GRIB field."""

    member: int
    lead_hours: int
    relative_path: str
    bytes_received: int
    elapsed_seconds: float
    grib_metadata: dict[str, int | float | str]


@dataclass(frozen=True, slots=True)
class _CandidateRequest:
    """All configuration shared while examining one advertised run."""

    coverage_id: str
    initialization: datetime
    token: str
    eccodes: _Eccodes
    leads: tuple[int, ...]
    run_dir: Path
    target: int
    step: int
    executed_at: datetime
    show_progress: bool


@dataclass(frozen=True, slots=True)
class _RunPlan:
    """Leads and output location shared by every candidate of one execution."""

    leads: tuple[int, ...]
    run_dir: Path
    executed_at: datetime


def required_leads(target_lead_hours: int, step_hours: int) -> tuple[int, ...]:
    """Return target and preceding leads at the selected interval.

    Returns:
        Requested forecast leads in descending order.

    Raises:
        ValueError: If the target lead or interval is invalid.

    """
    if not 0 <= target_lead_hours <= MAX_FORECAST_LEAD_HOURS:
        raise ValueError(f"target lead must be between 0 and {MAX_FORECAST_LEAD_HOURS} hours")
    if step_hours < 1:
        raise ValueError("time step must be positive")
    if target_lead_hours % LEAD_CADENCE_HOURS or step_hours % LEAD_CADENCE_HOURS:
        raise ValueError(
            f"lead time and step must be multiples of {LEAD_CADENCE_HOURS} hours "
            "(Z500 is only published every 3 h)"
        )
    return tuple(range(target_lead_hours, -1, -step_hours))


def _open_https(request: Request) -> _HttpResponse:
    """Open a request only to the pinned Météo-France HTTPS API host.

    Returns:
        Open HTTP response.

    Raises:
        RetrievalError: If the request does not target the pinned HTTPS host.

    """
    requested_url = urlsplit(request.full_url)
    api_url = urlsplit(API_BASE_URL)
    if requested_url.scheme != "https" or requested_url.netloc != api_url.netloc:
        raise RetrievalError("WCS request must target the configured HTTPS API host")
    # The request scheme and host are checked above; credentials never enter the URL.
    return cast(
        "_HttpResponse",
        urlopen(request, timeout=HTTP_TIMEOUT_SECONDS),  # nosec B310
    )


def read_token(env_file: Path) -> str:
    """Read the API token from the process environment or dotenv-style file.

    Returns:
        A non-empty API token.

    Raises:
        RetrievalError: If the configured token is missing or empty.

    """
    token = os.environ.get(VARIABLE_NAME)
    if token is not None and token:
        return token
    token = _read_token_from_file(env_file)
    if token is not None:
        return token
    raise RetrievalError(f"{VARIABLE_NAME} is missing or empty in {env_file}")


def _read_token_from_file(env_file: Path) -> str | None:
    """Find a non-empty API token in a dotenv-style file.

    Returns:
        The first matching token, or None when no non-empty token is found.

    """
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
        token = _parse_token_line(raw_line)
        if token is not None:
            return token
    return None


def _parse_token_line(raw_line: str) -> str | None:
    """Parse one dotenv-style line for the configured API token.

    Returns:
        The token value, or None when this line does not define a non-empty token.

    """
    line = raw_line.strip()
    line = _normalize_token_line(line)
    if line is None:
        return None
    key, separator, value = line.partition("=")
    if not separator or key.strip() != VARIABLE_NAME:
        return None
    token = value.strip().strip("\"'")
    return token or None


def _normalize_token_line(line: str) -> str | None:
    """Remove comments and an optional export prefix from a dotenv line.

    Returns:
        The normalized assignment, or None for a blank or comment line.

    """
    if not line or line.startswith("#"):
        return None
    if line.startswith("export "):
        return line.removeprefix("export ").lstrip()
    return line


def _local_name(element: _XmlElement) -> str:
    """Return an XML element name without its namespace.

    Returns:
        The unqualified element tag.

    """
    return element.tag.rsplit("}", 1)[-1]


def coverage_candidates(capabilities: bytes) -> tuple[tuple[str, datetime], ...]:
    """Parse and sort advertised Z500 coverages newest first.

    Returns:
        Coverage IDs and initialization times in descending time order.

    Raises:
        RetrievalError: If the capabilities XML or a matching coverage ID is invalid.

    """
    root = _parse_capabilities(capabilities)
    candidates = _parse_coverage_candidates(root)
    if not candidates:
        raise RetrievalError("No isobaric geopotential coverage was advertised")
    return tuple(sorted(candidates.items(), key=itemgetter(1), reverse=True))


def _parse_capabilities(capabilities: bytes) -> _XmlElement:
    """Parse a capabilities XML document.

    Returns:
        The parsed XML root element.

    Raises:
        RetrievalError: If the document is malformed.

    """
    try:
        return cast("_XmlElement", ElementTree.fromstring(capabilities))
    except ElementTree.ParseError as error:
        raise RetrievalError("GetCapabilities returned malformed XML") from error


def _parse_coverage_candidates(root: _XmlElement) -> dict[str, datetime]:
    """Extract valid PEARP coverage IDs and initialization times.

    Returns:
        Coverage IDs mapped to their initialization time.

    """
    candidates: dict[str, datetime] = {}
    for element in root.iter():
        candidate = _parse_coverage_element(element)
        if candidate is not None:
            coverage_id, initialization = candidate
            candidates[coverage_id] = initialization
    return candidates


def _parse_coverage_element(element: _XmlElement) -> tuple[str, datetime] | None:
    """Parse a matching coverage element, if this element advertises one.

    Returns:
        Coverage ID and initialization, or None for a different element.

    Raises:
        RetrievalError: If a matching coverage has an invalid timestamp.

    """
    coverage_id = (element.text or "").strip()
    if _local_name(element) != "CoverageId" or not coverage_id.startswith(COVERAGE_PREFIX):
        return None
    timestamp = coverage_id.removeprefix(COVERAGE_PREFIX)
    try:
        initialization = datetime.strptime(timestamp, "%Y-%m-%dT%H.%M.%SZ").replace(tzinfo=UTC)
    except ValueError as error:
        message = f"Invalid initialization timestamp in coverage ID {coverage_id!r}"
        raise RetrievalError(message) from error
    return coverage_id, initialization


def _load_eccodes() -> _Eccodes:
    """Load ecCodes with an actionable installation hint.

    Returns:
        Typed ecCodes API.

    Raises:
        RetrievalError: If the ecCodes Python binding cannot be imported.

    """
    try:
        module = importlib.import_module("eccodes")
    except ImportError as error:
        raise RetrievalError(
            "Retrieval requires ecCodes; install the project dependencies with `uv sync --locked`."
        ) from error
    return cast("_Eccodes", module)


def _validate_grib(
    body: bytes,
    eccodes: _Eccodes,
    *,
    member: int,
    lead_hours: int,
    initialization: datetime,
) -> dict[str, int | float | str]:
    """Validate a single complete Z500 GRIB response against its request.

    Returns:
        Validated GRIB metadata for this field.

    Raises:
        RetrievalError: If the response framing or requested field metadata is invalid.

    """
    _validate_grib_framing(body)
    metadata = _read_grib_metadata(body, eccodes)
    expected = _expected_grib_metadata(
        member=member,
        lead_hours=lead_hours,
        initialization=initialization,
    )
    mismatches = _metadata_mismatches(metadata, expected)
    if mismatches:
        raise RetrievalError(
            f"GRIB metadata mismatch for member {member}, lead {lead_hours}: {mismatches}"
        )
    return metadata


def _validate_grib_framing(body: bytes) -> None:
    """Ensure a response contains exactly one complete GRIB message.

    Raises:
        RetrievalError: If the message framing is invalid.

    """
    if len(body) < GRIB_MIN_MESSAGE_BYTES or body[:4] != b"GRIB" or body[-4:] != b"7777":
        raise RetrievalError("WCS response is not one complete GRIB message")
    message_length = int.from_bytes(body[8:16], "big")
    if message_length != len(body):
        raise RetrievalError(
            f"WCS returned {len(body)} bytes but GRIB header declares {message_length}"
        )


def _read_grib_metadata(
    body: bytes,
    eccodes: _Eccodes,
) -> dict[str, int | float | str]:
    """Read the identity and grid metadata from a GRIB message.

    Returns:
        Decoded GRIB metadata.

    """
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
        metadata = {key: eccodes.codes_get(handle, key) for key in keys}
    finally:
        eccodes.codes_release(handle)
    return metadata


def _expected_grib_metadata(
    *,
    member: int,
    lead_hours: int,
    initialization: datetime,
) -> dict[str, int | str]:
    """Build the expected metadata for the requested Z500 member field.

    Returns:
        Expected field identity, ensemble, and grid metadata.

    """
    return {
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


def _metadata_mismatches(
    actual: dict[str, int | float | str],
    expected: dict[str, int | str],
) -> dict[str, tuple[int | float | str | None, int | str]]:
    """Return expected metadata values that differ from the decoded message.

    Returns:
        Metadata keys mapped to their actual and expected values.

    """
    return {
        key: (actual.get(key), value) for key, value in expected.items() if actual.get(key) != value
    }


def _get_capabilities(token: str) -> bytes:
    """Retrieve and validate the capabilities XML.

    Returns:
        Bounded WCS capabilities XML.

    Raises:
        RetrievalError: If the request fails or the response is not XML.

    """
    query = urlencode({"service": "WCS", "version": "2.0.1", "language": "eng"})
    request = Request(
        f"{API_BASE_URL}{CAPABILITIES_PATH}?{query}",
        headers={"Accept": "application/xml", "Cache-Control": "no-cache"},
    )
    request.add_unredirected_header("apikey", token)
    try:
        response = _open_with_retries(request, context="GetCapabilities")
        with response:
            body = response.read(8 * 1024 * 1024 + 1)
    except OSError as error:
        raise RetrievalError(f"GetCapabilities request failed: {error}") from error
    if len(body) > 8 * 1024 * 1024 or b"<" not in body[:100]:
        raise RetrievalError("GetCapabilities response is not a bounded XML document")
    return body


def _next_access_time(error_body: str) -> datetime | None:
    """Parse supported WCS quota retry timestamp formats.

    Returns:
        Parsed retry time, or None when the response has no valid time.

    """
    match = _THROTTLE_PATTERN.search(error_body)
    if match is None:
        return None
    raw_value = match.group(1)
    value = raw_value.replace(" UTC", "").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        normalized = re.sub(r"([A-Za-z]{3})\.", r"\1", raw_value.replace(" UTC", ""))
        try:
            return datetime.strptime(normalized, "%Y-%b-%d %H:%M:%S%z")
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _wait_for_request_slot(context: str) -> None:
    """Reserve a smoothed request slot under the rolling-minute quota.

    The local budget stays below the documented service limit to leave room
    for requests from other processes sharing the same account.

    """
    with _request_lock:
        while True:
            now = monotonic()
            _discard_expired_request_timestamps(now)
            delay = _request_slot_delay(now)
            if delay <= 0:
                _request_timestamps.append(now)
                return
            delay += SCHEDULING_EPSILON_SECONDS
            logger.debug(
                "Waiting %.3f s before WCS request %s to stay within %d requests/min",
                delay,
                context,
                MAX_REQUESTS_PER_MINUTE,
            )
            sleep(delay)


def _discard_expired_request_timestamps(now: float) -> None:
    """Remove request timestamps older than the rolling quota window."""
    cutoff = now - REQUEST_WINDOW_SECONDS
    while _request_timestamps and _request_timestamps[0] <= cutoff:
        _request_timestamps.popleft()


def _request_slot_delay(now: float) -> float:
    """Return the wait required by both the rolling quota and request spacing.

    Returns:
        Seconds to wait before starting another request.

    """
    window_wait = (
        _request_timestamps[0] + REQUEST_WINDOW_SECONDS - now
        if len(_request_timestamps) >= MAX_REQUESTS_PER_MINUTE
        else 0.0
    )
    interval_wait = (
        _request_timestamps[-1] + MIN_REQUEST_INTERVAL_SECONDS - now if _request_timestamps else 0.0
    )
    return max(window_wait, interval_wait)


def _open_wcs_request(request: Request, *, context: str) -> _HttpResponse:
    """Open one WCS request after reserving a rate-limited request slot.

    Returns:
        Open HTTP response.

    """
    _wait_for_request_slot(context)
    return _open_https(request)


def _sleep_before_throttle_retry(
    error: HTTPError,
    *,
    context: str,
    retry_count: int,
) -> None:
    """Wait for a valid service-directed retry, or raise when it is unsafe.

    Raises:
        RetrievalError: If the failure is not a retryable throttle response.

    """
    if error.code != HTTP_TOO_MANY_REQUESTS:
        raise RetrievalError(f"HTTP {error.code} for {context}") from error

    error_body = error.read(4096).decode("utf-8", errors="replace")
    retry_time = _next_access_time(error_body)
    if retry_time is None:
        raise RetrievalError(
            f"HTTP 429 for {context}; response did not include a valid "
            "nextAccessTime, so the request cannot be safely retried"
        ) from error
    if retry_count >= MAX_THROTTLE_RETRIES:
        raise RetrievalError(
            f"HTTP 429 for {context} after {retry_count} throttle retries"
        ) from error

    remaining = retry_time - datetime.now(UTC)
    delay = max(0.0, remaining.total_seconds()) + 1.0
    logger.warning(
        "WCS throttled %s; retry %d/%d in %.1f s",
        context,
        retry_count + 1,
        MAX_THROTTLE_RETRIES,
        delay,
    )
    sleep(delay)


def _sleep_before_network_retry(
    error: OSError,
    *,
    context: str,
    retry_count: int,
) -> None:
    """Apply bounded backoff for transient network errors.

    Raises:
        RetrievalError: If the network retry limit has been reached.

    """
    if retry_count >= MAX_NETWORK_RETRIES:
        raise RetrievalError(
            f"Network failure for {context} after {retry_count} retries: {error}"
        ) from error

    retry_number = retry_count + 1
    delay = float(retry_number)
    logger.warning(
        "Network retry %d/%d for %s in %.1f s",
        retry_number,
        MAX_NETWORK_RETRIES,
        context,
        delay,
    )
    sleep(delay)


def _open_with_retries(
    request: Request,
    *,
    context: str,
) -> _HttpResponse:
    """Open a WCS request with bounded network and service-directed retries.

    Returns:
        Open response for the requested field.

    """
    throttle_retries = 0
    network_retries = 0
    while True:
        try:
            return _open_wcs_request(request, context=context)
        except HTTPError as error:
            _sleep_before_throttle_retry(
                error,
                context=context,
                retry_count=throttle_retries,
            )
            throttle_retries += 1
        except OSError as error:
            _sleep_before_network_retry(
                error,
                context=context,
                retry_count=network_retries,
            )
            network_retries += 1


def _request_field(
    token: str,
    coverage_id: str,
    member: int,
    lead_hours: int,
) -> bytes:
    """Retrieve one full-grid global Z500 field.

    Returns:
        Bounded GRIB response body.

    """
    request = _coverage_request(token, coverage_id, member, lead_hours)
    response = _open_with_retries(
        request,
        context=f"GetCoverage member {member:03d}, lead {lead_hours} h",
    )
    return _read_grib_response(response, member=member, lead_hours=lead_hours)


def _coverage_request(token: str, coverage_id: str, member: int, lead_hours: int) -> Request:
    """Build the authenticated GetCoverage request for one Z500 field.

    Returns:
        The HTTP request with the coverage query and API key.

    """
    endpoint = f"/wcs/MF-NWP-GLOBAL-PEARP{member:03}-025-GLOBE-WCS/GetCoverage"
    parameters = urlencode(
        [
            ("service", "WCS"),
            ("version", "2.0.1"),
            ("coverageid", coverage_id),
            ("format", "application/wmo-grib"),
            ("subset", "pressure(500)"),
            ("subset", f"time({lead_hours * 3600})"),
        ]
    )
    request = Request(
        f"{API_BASE_URL}{endpoint}?{parameters}",
        headers={"Accept": "application/wmo-grib", "Cache-Control": "no-cache"},
    )
    request.add_unredirected_header("apikey", token)
    return request


def _read_grib_response(
    response: _HttpResponse,
    *,
    member: int,
    lead_hours: int,
) -> bytes:
    """Read one bounded response and validate its declared size and content type.

    Returns:
        The validated GRIB message body.

    """
    with response:
        content_type = response.headers.get("Content-Type", "unknown")
        content_length = response.headers.get("Content-Length")
        body = response.read(MAX_RESPONSE_BYTES + 1)
    _validate_content_length(content_length, member=member, lead_hours=lead_hours)
    _validate_grib_content(body, content_type, member=member, lead_hours=lead_hours)
    return body


def _validate_content_length(
    content_length: str | None,
    *,
    member: int,
    lead_hours: int,
) -> None:
    """Reject malformed or oversized declared response lengths.

    Raises:
        RetrievalError: If the declared length is invalid or too large.

    """
    if content_length is None:
        return
    if not content_length:
        return
    try:
        declared_length = int(content_length)
    except ValueError as error:
        raise RetrievalError(
            f"Invalid Content-Length {content_length!r} for member {member:03}, "
            f"lead {lead_hours} h",
        ) from error
    if declared_length > MAX_RESPONSE_BYTES:
        raise RetrievalError(
            f"Response Content-Length exceeds {MAX_RESPONSE_BYTES} bytes "
            f"for member {member:03}, lead {lead_hours} h"
        )


def _validate_grib_content(
    body: bytes,
    content_type: str,
    *,
    member: int,
    lead_hours: int,
) -> None:
    """Reject oversized or non-GRIB content for a requested field.

    Raises:
        RetrievalError: If the body is too large or is not GRIB content.

    """
    if len(body) > MAX_RESPONSE_BYTES:
        raise RetrievalError(
            f"Response exceeds {MAX_RESPONSE_BYTES} bytes for member {member:03}, "
            f"lead {lead_hours} h"
        )
    if "xml" in content_type.casefold():
        raise RetrievalError(
            f"Expected GRIB but received {content_type} for member {member:03}, lead {lead_hours} h"
        )


def run_directory_name(executed_at: datetime, target_lead_hours: int, step_hours: int) -> str:
    """Name the output directory of one execution.

    Example: ``run_2026-10-30-09-59-Z_lead_time_24_step_24``.

    Returns:
        Directory name based on the UTC execution minute and the requested hours.

    """
    stamp = f"{executed_at.astimezone(UTC):%Y-%m-%d-%H-%M}-Z"
    return f"run_{stamp}_lead_time_{target_lead_hours}_step_{step_hours}"


def _write_manifest(run_dir: Path, manifest: Mapping[str, object]) -> None:
    """Atomically persist required run traceability."""
    path = run_dir / "manifest.json"
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _fetch_field(
    candidate: _CandidateRequest,
    *,
    member: int,
    lead: int,
    staging_dir: Path,
) -> FieldRecord:
    """Fetch one member/lead GRIB message, validate it, and store it atomically.

    Returns:
        Traceability record of the validated field.

    """
    started = monotonic()
    relative_path = f"member_{member:03}_lead_{lead:03}.grib"
    body = _request_field(candidate.token, candidate.coverage_id, member, lead)
    metadata = _validate_grib(
        body,
        candidate.eccodes,
        member=member,
        lead_hours=lead,
        initialization=candidate.initialization,
    )
    field_path = staging_dir / relative_path
    temporary_path = field_path.with_suffix(".grib.tmp")
    temporary_path.write_bytes(body)
    temporary_path.replace(field_path)
    return FieldRecord(
        member=member,
        lead_hours=lead,
        relative_path=relative_path,
        bytes_received=len(body),
        elapsed_seconds=round(monotonic() - started, 3),
        grib_metadata=metadata,
    )


def _fetch_candidate_fields(candidate: _CandidateRequest, staging_dir: Path) -> list[FieldRecord]:
    """Retrieve and validate every requested member/lead field of one candidate.

    Returns:
        One record per field, in retrieval order.

    """
    label = f"WCS {candidate.initialization:%Y-%m-%d %H} UTC"
    records: list[FieldRecord] = []
    with tqdm(
        total=len(candidate.leads) * len(MEMBERS),
        desc=label,
        unit="field",
        dynamic_ncols=True,
        disable=not candidate.show_progress,
    ) as progress:
        for lead, member in product(candidate.leads, MEMBERS):
            progress.set_description(f"{label} | Z500 | t+{lead}h | member {member:03}")
            record = _fetch_field(candidate, member=member, lead=lead, staging_dir=staging_dir)
            records.append(record)
            progress.update()
            logger.debug(
                "Validated variable=Z500 lead=%03dh member=%03d bytes=%d",
                lead,
                member,
                record.bytes_received,
            )
    return records


def _manifest(candidate: _CandidateRequest, records: list[FieldRecord]) -> dict[str, object]:
    """Describe a complete candidate retrieval for later stages.

    Returns:
        JSON-serializable manifest including request identity and fetch metrics.

    """
    return {
        "coverage_id": candidate.coverage_id,
        "initialization_utc": candidate.initialization.isoformat(),
        "executed_at_utc": candidate.executed_at.astimezone(UTC).isoformat(),
        "target_lead_hours": candidate.target,
        "time_step_hours": candidate.step,
        "required_leads_hours": list(candidate.leads),
        "members": list(MEMBERS),
        "status": "complete",
        "source": "Météo-France PE-ARPEGE WCS GLOB025",
        "variable": "Z500",
        "units": "m**2 s**-2",
        "fields": [asdict(record) for record in records],
        "elapsed_seconds": round(sum(record.elapsed_seconds for record in records), 3),
        "bytes_received": sum(record.bytes_received for record in records),
    }


def _run_candidate(candidate: _CandidateRequest) -> Path:
    """Fetch a candidate into a staging directory and publish it only when complete.

    Returns:
        The ``raw_fetch`` directory holding the GRIB fields and the manifest.

    """
    staging_dir = candidate.run_dir / f".attempt_{candidate.initialization:%Y%m%d%H}"
    staging_dir.mkdir()
    try:
        records = _fetch_candidate_fields(candidate, staging_dir)
        _write_manifest(staging_dir, _manifest(candidate, records))
        raw_fetch_dir = candidate.run_dir / RAW_FETCH_DIRNAME
        staging_dir.replace(raw_fetch_dir)
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)
    return raw_fetch_dir


def retrieve_latest_complete_run(options: RetrievalOptions) -> Path:
    """Retrieve the newest advertised run complete for every required field.

    The run is written to ``<output_dir>/run_<execution time>_lead_time_<lead>_step_<step>``;
    its ``raw_fetch`` subdirectory holds the validated GRIB files and ``manifest.json``.
    Nothing is left on disk when no candidate run is complete.

    Returns:
        The run directory containing ``raw_fetch``.

    Raises:
        RetrievalError: If no candidate is complete for the requested fields.

    """
    leads = required_leads(options.target_lead_hours, options.step_hours)
    token = read_token(options.env_file)
    eccodes = _load_eccodes()
    candidates = _candidate_runs(options, token)
    plan = _create_run_plan(options, leads)
    try:
        raw_fetch_dir, failures = _try_candidate_runs(options, token, candidates, plan, eccodes)
    except BaseException:
        shutil.rmtree(plan.run_dir, ignore_errors=True)
        raise
    if raw_fetch_dir is None:
        shutil.rmtree(plan.run_dir, ignore_errors=True)
        summary = _candidate_failure_summary(failures)
        raise RetrievalError(f"No complete WCS run found for required leads {leads}: {summary}")
    return plan.run_dir


def _create_run_plan(options: RetrievalOptions, leads: tuple[int, ...]) -> _RunPlan:
    """Create the execution-specific output directory.

    Returns:
        Required leads, new run directory, and execution time.

    Raises:
        RetrievalError: If the directory for this execution minute already exists.

    """
    executed_at = options.executed_at or datetime.now(UTC)
    run_dir = options.output_dir / run_directory_name(
        executed_at, options.target_lead_hours, options.step_hours
    )
    try:
        run_dir.mkdir(parents=True)
    except FileExistsError as error:
        raise RetrievalError(
            f"Output directory already exists; retry in a minute: {run_dir}"
        ) from error
    return _RunPlan(leads=leads, run_dir=run_dir, executed_at=executed_at)


def _candidate_runs(
    options: RetrievalOptions,
    token: str,
) -> tuple[tuple[str, datetime], ...]:
    """Fetch advertised candidate runs and apply an optional coverage filter.

    Returns:
        Advertised candidates, newest first, filtered to the requested ID.

    Raises:
        RetrievalError: If the requested coverage ID was not advertised.

    """
    candidates = coverage_candidates(_get_capabilities(token))
    if options.coverage_id is None:
        return candidates
    selected = tuple(item for item in candidates if item[0] == options.coverage_id)
    if not selected:
        raise RetrievalError(f"Requested coverage ID was not advertised: {options.coverage_id}")
    return selected


def _try_candidate_runs(
    options: RetrievalOptions,
    token: str,
    candidates: tuple[tuple[str, datetime], ...],
    plan: _RunPlan,
    eccodes: _Eccodes,
) -> tuple[Path | None, list[str]]:
    """Try candidates in order, recording expected retrieval failures.

    Returns:
        ``raw_fetch`` directory of the first complete candidate, plus candidate failures.

    """
    failures: list[str] = []
    for coverage_id, initialization in candidates:
        logger.info("Checking WCS run %s for leads %s", coverage_id, plan.leads)
        try:
            raw_fetch_dir = _run_candidate(
                _make_candidate_request(
                    options,
                    token,
                    (coverage_id, initialization),
                    plan,
                    eccodes,
                )
            )
        except RetrievalError as error:
            failures.append(f"{coverage_id}: {error}")
            logger.warning("Candidate run incomplete: %s", failures[-1])
        else:
            return raw_fetch_dir, failures
    return None, failures


def _make_candidate_request(
    options: RetrievalOptions,
    token: str,
    candidate: tuple[str, datetime],
    plan: _RunPlan,
    eccodes: _Eccodes,
) -> _CandidateRequest:
    """Combine retrieval options and advertised metadata into a candidate.

    Returns:
        Request details for one advertised candidate run.

    """
    coverage_id, initialization = candidate
    return _CandidateRequest(
        coverage_id=coverage_id,
        initialization=initialization,
        token=token,
        eccodes=eccodes,
        leads=plan.leads,
        run_dir=plan.run_dir,
        target=options.target_lead_hours,
        step=options.step_hours,
        executed_at=plan.executed_at,
        show_progress=options.show_progress,
    )


def _candidate_failure_summary(failures: list[str]) -> str:
    """Format candidate failures, including the no-candidates case.

    Returns:
        A concise string suitable for the final retrieval error.

    """
    return "; ".join(failures) if failures else "no candidate runs found"
