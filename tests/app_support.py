# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Shared synthetic run and grid for the application tests."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING

import numpy as np

from ensemble_sensitivity.app import model
from ensemble_sensitivity.run_index import new_run_id, register_run

if TYPE_CHECKING:
    from pathlib import Path

    import pytest

LEADS = (0, 3, 6, 9)
INITIALIZATION = datetime(2026, 1, 1, 0, tzinfo=UTC)


def make_run(root: Path, day: int = 1, leads: tuple[int, ...] = LEADS) -> str:
    run_id = new_run_id()
    raw = root / run_id / "raw_fetch"
    raw.mkdir(parents=True)
    (raw / "manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "fields": [{"member": 0, "lead_hours": lead} for lead in leads],
                "coverage_id": "cov",
                "initialization_utc": INITIALIZATION.isoformat(),
                "run_id": run_id,
            }
        ),
        encoding="utf-8",
    )
    register_run(root, run_id, {"coverage_id": "cov"}, datetime(2026, 1, day, tzinfo=UTC))
    return run_id


def patch_synthetic_grid(monkeypatch: pytest.MonkeyPatch) -> None:
    rng = np.random.default_rng(5)
    base = rng.normal(5500.0, 30.0, size=(35, 21, 20))

    def loader(*_args: object) -> object:
        return lambda member, lead: base[member] + lead * (1.0 + member / 35.0)

    monkeypatch.setattr(model, "run_field_loader", loader)
    monkeypatch.setattr(
        model, "grid_axes", lambda: (np.linspace(60.0, 40.0, 21), np.arange(340.0, 360.0))
    )
