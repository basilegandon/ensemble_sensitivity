# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Retrieve and validate complete PEARP Z500 runs from the Météo-France WCS API."""

from __future__ import annotations

import importlib
import json
import logging
import os
import re
import time
from collections import deque
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from operator import itemgetter
from threading import Lock
from typing import TYPE_CHECKING, Protocol, Self, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from defusedxml import ElementTree
from tqdm import tqdm

if TYPE_CHECKING:
    from collections.abc import Iterator
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

    def read(self, amount: int = -1, /) -> bytes:
        """Read response bytes."""

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
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


@dataclass(frozen=True, slots=True)
class FieldRecord:
    """Traceability record for one saved member/lead GRIB field."""

    member: int
    lead_hours: int
    relative_path: str
    bytes_received: int
    elapsed_seconds: float
    reused: bool
    grib_metadata: dict[str, int | float | str]


@dataclass(frozen=True, slots=True)
class _CandidateRequest:
    """All configuration shared while examining one advertised run."""

    coverage_id: str
    initialization: datetime
    token: str
    eccodes: _Eccodes
    leads: tuple[int, ...]
    output_dir: Path
    target: int
    step: int


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
    if token:
        return token
    for raw_line in env_file.read_text(encoding="utf-8").splitlines():
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
    raise RetrievalError(f"{VARIABLE_NAME} is missing or empty in {env_file}")


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
    try:
        root = cast("_XmlElement", ElementTree.fromstring(capabilities))
    except ElementTree.ParseError as error:
        raise RetrievalError("GetCapabilities returned malformed XML") from error

    candidates: dict[str, datetime] = {}
    for element in root.iter():
        coverage_id = (element.text or "").strip()
        if _local_name(element) != "CoverageId" or not coverage_id.startswith(COVERAGE_PREFIX):
            continue
        timestamp = coverage_id.removeprefix(COVERAGE_PREFIX)
        try:
            initialization = datetime.strptime(timestamp, "%Y-%m-%dT%H.%M.%SZ").replace(tzinfo=UTC)
        except ValueError as error:
            message = f"Invalid initialization timestamp in coverage ID {coverage_id!r}"
            raise RetrievalError(message) from error
        candidates[coverage_id] = initialization
    if not candidates:
        raise RetrievalError("No isobaric geopotential coverage was advertised")
    return tuple(sorted(candidates.items(), key=itemgetter(1), reverse=True))


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
    if len(body) < GRIB_MIN_MESSAGE_BYTES or body[:4] != b"GRIB" or body[-4:] != b"7777":
        raise RetrievalError("WCS response is not one complete GRIB message")
    message_length = int.from_bytes(body[8:16], "big")
    if message_length != len(body):
        raise RetrievalError(
            f"WCS returned {len(body)} bytes but GRIB header declares {message_length}"
        )
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
        raise RetrievalError(
            f"GRIB metadata mismatch for member {member}, lead {lead_hours}: {mismatches}"
        )
    return metadata


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
    except (HTTPError, URLError, TimeoutError, OSError) as error:
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
            now = time.monotonic()
            cutoff = now - REQUEST_WINDOW_SECONDS
            while _request_timestamps and _request_timestamps[0] <= cutoff:
                _request_timestamps.popleft()
            window_wait = (
                _request_timestamps[0] + REQUEST_WINDOW_SECONDS - now
                if len(_request_timestamps) >= MAX_REQUESTS_PER_MINUTE
                else 0.0
            )
            interval_wait = (
                _request_timestamps[-1] + MIN_REQUEST_INTERVAL_SECONDS - now
                if _request_timestamps
                else 0.0
            )
            delay = max(window_wait, interval_wait)
            if delay <= 0:
                _request_timestamps.append(now)
                return
            delay += SCHEDULING_EPSILON_SECONDS
            logger.info(
                "Waiting %.3f s before WCS request %s to stay within %d requests/min",
                delay,
                context,
                MAX_REQUESTS_PER_MINUTE,
            )
            time.sleep(delay)


def _open_wcs_request(request: Request, *, context: str) -> _HttpResponse:
    """Open one WCS request after reserving a rate-limited request slot.

    Returns:
        Open HTTP response.

    """
    _wait_for_request_slot(context)
    return _open_https(request)


def _open_with_retries(
    request: Request,
    *,
    context: str,
) -> _HttpResponse:
    """Open a WCS request with bounded network and service-directed retries.

    Returns:
        Open response for the requested field.

    Raises:
        RetrievalError: If HTTP or network retries are exhausted.

    """
    throttle_retries = 0
    network_retries = 0
    while True:
        try:
            return _open_wcs_request(request, context=context)
        except HTTPError as error:
            retry_time = (
                _next_access_time(error.read(4096).decode("utf-8", errors="replace"))
                if error.code == HTTP_TOO_MANY_REQUESTS
                else None
            )
            if retry_time is None:
                if error.code == HTTP_TOO_MANY_REQUESTS:
                    message = (
                        f"HTTP 429 for {context}; response did not include a valid "
                        "nextAccessTime, so the request cannot be safely retried"
                    )
                else:
                    message = f"HTTP {error.code} for {context}"
                raise RetrievalError(message) from error
            if throttle_retries >= MAX_THROTTLE_RETRIES:
                raise RetrievalError(
                    f"HTTP 429 for {context} after {throttle_retries} throttle retries"
                ) from error
            delay = max(0.0, (retry_time - datetime.now(UTC)).total_seconds()) + 1.0
            throttle_retries += 1
            logger.warning(
                "WCS throttled %s; retry %d/%d in %.1f s",
                context,
                throttle_retries,
                MAX_THROTTLE_RETRIES,
                delay,
            )
            time.sleep(delay)
        except (TimeoutError, OSError) as error:
            if network_retries >= MAX_NETWORK_RETRIES:
                raise RetrievalError(
                    f"Network failure for {context} after {network_retries} retries: {error}"
                ) from error
            network_retries += 1
            delay = float(network_retries)
            logger.warning(
                "Network retry %d/%d for %s in %.1f s",
                network_retries,
                MAX_NETWORK_RETRIES,
                context,
                delay,
            )
            time.sleep(delay)


def _request_field(
    token: str,
    coverage_id: str,
    member: int,
    lead_hours: int,
) -> bytes:
    """Retrieve one full-grid global Z500 field.

    Returns:
        Bounded GRIB response body.

    Raises:
        RetrievalError: If transport or response checks fail.

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
        headers={
            "Accept": "application/wmo-grib",
            "Cache-Control": "no-cache",
        },
    )
    request.add_unredirected_header("apikey", token)
    response = _open_with_retries(
        request,
        context=f"GetCoverage member {member:03d}, lead {lead_hours} h",
    )
    with response:
        content_type = response.headers.get("Content-Type", "unknown")
        content_length = response.headers.get("Content-Length")
        if content_length:
            try:
                declared_length = int(content_length)
            except ValueError as error:
                message = (
                    f"Invalid Content-Length {content_length!r} for member {member:03}, "
                    f"lead {lead_hours} h"
                )
                raise RetrievalError(message) from error
            if declared_length > MAX_RESPONSE_BYTES:
                raise RetrievalError(
                    f"Response Content-Length exceeds {MAX_RESPONSE_BYTES} bytes "
                    f"for member {member:03}, lead {lead_hours} h"
                )
        body = response.read(MAX_RESPONSE_BYTES + 1)
    if len(body) > MAX_RESPONSE_BYTES:
        raise RetrievalError(
            f"Response exceeds {MAX_RESPONSE_BYTES} bytes for member {member:03}, "
            f"lead {lead_hours} h"
        )
    if "xml" in content_type.casefold():
        raise RetrievalError(
            f"Expected GRIB but received {content_type} for member {member:03}, lead {lead_hours} h"
        )
    return body


def _candidate_dir(output_dir: Path, initialization: datetime, target: int, step: int) -> Path:
    """Build a portable output directory name for a run and request.

    Returns:
        Candidate run output directory.

    """
    name = f"run_{initialization:%Y%m%d%H}_t{target}_s{step}"
    return output_dir / name


def _write_manifest(run_dir: Path, manifest: dict[str, object]) -> None:
    """Atomically persist required run traceability."""
    path = run_dir / "manifest.json"
    temporary_path = path.with_suffix(".json.tmp")
    temporary_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary_path.replace(path)


def _obtain_field(
    candidate: _CandidateRequest,
    *,
    member: int,
    lead: int,
    field_path: Path,
) -> tuple[bytes, dict[str, int | float | str], bool]:
    """Validate a cache field or fetch, validate, and atomically store it.

    Returns:
        GRIB bytes, validated metadata, and whether the field was reused.

    """
    reused = field_path.is_file()
    if reused:
        body = field_path.read_bytes()
        try:
            metadata = _validate_grib(
                body,
                candidate.eccodes,
                member=member,
                lead_hours=lead,
                initialization=candidate.initialization,
            )
        except RetrievalError:
            logger.warning("Cached GRIB field failed validation; fetching again: %s", field_path)
            reused = False
    if not reused:
        body = _request_field(candidate.token, candidate.coverage_id, member, lead)
        metadata = _validate_grib(
            body,
            candidate.eccodes,
            member=member,
            lead_hours=lead,
            initialization=candidate.initialization,
        )
        temporary_path = field_path.with_suffix(".grib.tmp")
        temporary_path.write_bytes(body)
        temporary_path.replace(field_path)
    return body, metadata, reused


def _run_candidate(candidate: _CandidateRequest) -> Path:
    """Fetch/resume a candidate and mark it complete only after all fields pass.

    Returns:
        Directory containing the complete run fields and manifest.

    Raises:
        RetrievalError: If the request identity or a required field is invalid.

    """
    run_dir = _candidate_dir(
        candidate.output_dir,
        candidate.initialization,
        candidate.target,
        candidate.step,
    )
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = run_dir / "manifest.json"
    request_identity = {
        "coverage_id": candidate.coverage_id,
        "initialization_utc": candidate.initialization.isoformat(),
        "target_lead_hours": candidate.target,
        "time_step_hours": candidate.step,
        "required_leads_hours": list(candidate.leads),
        "members": list(MEMBERS),
        "status": "in_progress",
        "source": "Météo-France PE-ARPEGE WCS GLOB025",
        "variable": "Z500",
        "units": "m**2 s**-2",
        "fields": [],
    }
    if manifest_path.exists():
        old_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not isinstance(old_manifest, dict):
            raise RetrievalError(f"Existing manifest is not a JSON object: {manifest_path}")
        for key in (
            "coverage_id",
            "target_lead_hours",
            "time_step_hours",
            "required_leads_hours",
            "members",
        ):
            if old_manifest.get(key) != request_identity[key]:
                raise RetrievalError(f"Existing manifest has different request identity: {key}")

    records: list[FieldRecord] = []
    _write_manifest(run_dir, request_identity)
    total_fields = len(candidate.leads) * len(MEMBERS)
    with tqdm(
        total=total_fields,
        desc=f"WCS {candidate.initialization:%Y-%m-%d %H} UTC",
        unit="field",
        dynamic_ncols=True,
    ) as progress:
        for lead in candidate.leads:
            for member in MEMBERS:
                progress.set_description(
                    f"WCS {candidate.initialization:%Y-%m-%d %H} UTC "
                    f"| Z500 | t+{lead}h | member {member:03}"
                )
                started = time.monotonic()
                relative_path = f"member_{member:03}_lead_{lead:03}.grib"
                field_path = run_dir / relative_path
                try:
                    body, metadata, reused = _obtain_field(
                        candidate,
                        member=member,
                        lead=lead,
                        field_path=field_path,
                    )
                except RetrievalError as error:
                    request_identity["status"] = "failed"
                    request_identity["failure"] = str(error)
                    request_identity["fields"] = [asdict(item) for item in records]
                    request_identity["elapsed_seconds"] = round(
                        sum(record.elapsed_seconds for record in records), 3
                    )
                    request_identity["bytes_received"] = sum(
                        record.bytes_received for record in records if not record.reused
                    )
                    _write_manifest(run_dir, request_identity)
                    raise
                record = FieldRecord(
                    member=member,
                    lead_hours=lead,
                    relative_path=relative_path,
                    bytes_received=len(body),
                    elapsed_seconds=round(time.monotonic() - started, 3),
                    reused=reused,
                    grib_metadata=metadata,
                )
                records.append(record)
                request_identity["fields"] = [asdict(item) for item in records]
                _write_manifest(run_dir, request_identity)
                progress.update()
                logger.info(
                    "Validated variable=Z500 lead=%03dh member=%03d bytes=%d reused=%s",
                    lead,
                    member,
                    len(body),
                    reused,
                )
    request_identity["status"] = "complete"
    request_identity["elapsed_seconds"] = round(
        sum(record.elapsed_seconds for record in records), 3
    )
    request_identity["bytes_received"] = sum(
        record.bytes_received for record in records if not record.reused
    )
    _write_manifest(run_dir, request_identity)
    return run_dir


def retrieve_latest_complete_run(options: RetrievalOptions) -> Path:
    """Retrieve the newest advertised run complete for every required field.

    Returns:
        Directory containing the complete run fields and manifest.

    Raises:
        RetrievalError: If no candidate is complete for the requested fields.

    """
    leads = required_leads(options.target_lead_hours, options.step_hours)
    token = read_token(options.env_file)
    candidates = coverage_candidates(_get_capabilities(token))
    if options.coverage_id is not None:
        candidates = tuple(item for item in candidates if item[0] == options.coverage_id)
        if not candidates:
            raise RetrievalError(f"Requested coverage ID was not advertised: {options.coverage_id}")
    eccodes = _load_eccodes()
    failures: list[str] = []
    for coverage_id, initialization in candidates:
        logger.info("Checking WCS run %s for leads %s", coverage_id, leads)
        try:
            return _run_candidate(
                _CandidateRequest(
                    coverage_id=coverage_id,
                    initialization=initialization,
                    token=token,
                    eccodes=eccodes,
                    leads=leads,
                    output_dir=options.output_dir,
                    target=options.target_lead_hours,
                    step=options.step_hours,
                )
            )
        except RetrievalError as error:
            failures.append(f"{coverage_id}: {error}")
            logger.warning("Candidate run incomplete: %s", failures[-1])
    summary = "; ".join(failures) if failures else "no candidate runs found"
    raise RetrievalError(f"No complete WCS run found for required leads {leads}: {summary}")
