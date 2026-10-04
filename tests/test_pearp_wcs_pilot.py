# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the PEARP WCS batch-pilot selection and validation."""

from datetime import UTC, datetime

import pytest

from docs.pilot_pearp_wcs import (
    PilotError,
    latest_coverage_id,
    next_access_time,
    required_leads,
    validate_metadata,
)


@pytest.mark.parametrize(
    ("target_lead_hours", "step_hours", "expected"),
    [
        (24, 24, [24, 0]),
        (72, 24, [72, 48, 24, 0]),
        (102, 24, [102, 78, 54, 30, 6]),
    ],
)
def test_required_leads_follow_analysis_step(
    target_lead_hours: int,
    step_hours: int,
    expected: list[int],
) -> None:
    """Return the target and preceding leads at the analysis interval."""
    assert required_leads(target_lead_hours, step_hours) == expected


@pytest.mark.parametrize(
    ("target_lead_hours", "step_hours", "message"),
    [
        (-1, 24, "negative target lead"),
        (103, 24, "API maximum"),
        (24, 0, "non-positive time step"),
        (24, -1, "non-positive time step"),
    ],
)
def test_required_leads_reject_invalid_inputs(
    target_lead_hours: int,
    step_hours: int,
    message: str,
) -> None:
    """Reject a negative target or a non-positive step."""
    with pytest.raises(ValueError, match=message):
        required_leads(target_lead_hours, step_hours)


def test_latest_coverage_id_uses_newest_initialization() -> None:
    """Choose the newest Z500 coverage while ignoring other variables."""
    capabilities = b"""\
    <Capabilities>
      <CoverageId>GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-03T18.00.00Z</CoverageId>
      <CoverageId>GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-04T00.00.00Z</CoverageId>
      <CoverageId>TEMPERATURE__ISOBARIC_SURFACE___2026-10-04T06.00.00Z</CoverageId>
    </Capabilities>
    """
    assert latest_coverage_id(capabilities) == (
        "GEOPOTENTIAL__ISOBARIC_SURFACE___2026-10-04T00.00.00Z"
    )


def test_latest_coverage_id_rejects_missing_candidate() -> None:
    """Fail clearly when capabilities contain no Z500 coverage."""
    with pytest.raises(PilotError, match="No isobaric geopotential"):
        latest_coverage_id(b"<Capabilities />")


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
    ],
)
def test_next_access_time_parses_api_retry_formats(body: str, expected: datetime | None) -> None:
    """Parse documented retry timestamps and ignore missing retry metadata."""
    assert next_access_time(body) == expected


def test_metadata_validation_requires_exact_run_member_and_global_grid() -> None:
    """Validate complete-grid member identity and reject a mismatched member."""
    metadata: dict[str, int | float | str] = {
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
    validate_metadata(
        metadata,
        member=7,
        lead_hours=24,
        initialization=datetime(2026, 10, 4, tzinfo=UTC),
    )

    metadata["number"] = 6
    with pytest.raises(PilotError, match="metadata mismatch"):
        validate_metadata(
            metadata,
            member=7,
            lead_hours=24,
            initialization=datetime(2026, 10, 4, tzinfo=UTC),
        )
