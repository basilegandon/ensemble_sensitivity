# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the global run index."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from uuid import UUID

import pytest

from ensemble_sensitivity import run_index
from ensemble_sensitivity.run_index import RunIndexError

if TYPE_CHECKING:
    from pathlib import Path


def test_new_run_id_is_a_unique_uuid4() -> None:
    first, second = run_index.new_run_id(), run_index.new_run_id()

    assert first != second
    assert UUID(first).version == 4


def test_missing_index_loads_as_empty(tmp_path: Path) -> None:
    assert run_index.load_index(tmp_path) == {}
    assert run_index.prune_index(tmp_path) == ()
    assert not run_index.index_path(tmp_path).exists()


def test_register_round_trips_parameters_atomically(tmp_path: Path) -> None:
    (tmp_path / "abc").mkdir()
    created = datetime(2026, 10, 4, 8, 5, tzinfo=UTC)

    run_index.register_run(tmp_path, "abc", {"target_lead_hours": 24}, created)

    entry = run_index.load_index(tmp_path)["abc"]
    assert entry == {
        "directory": "abc",
        "created_at_utc": "2026-10-04T08:05:00+00:00",
        "parameters": {"target_lead_hours": 24},
    }
    assert [path.name for path in tmp_path.iterdir() if path.is_file()] == [
        run_index.INDEX_FILENAME
    ]


def test_register_rejects_duplicate_and_missing_directory(tmp_path: Path) -> None:
    (tmp_path / "abc").mkdir()
    run_index.register_run(tmp_path, "abc", {})

    with pytest.raises(RunIndexError, match="already indexed"):
        run_index.register_run(tmp_path, "abc", {})
    with pytest.raises(RunIndexError, match="does not exist"):
        run_index.register_run(tmp_path, "nope", {})


def test_prune_removes_only_entries_without_directory(tmp_path: Path) -> None:
    for name in ("kept", "gone"):
        (tmp_path / name).mkdir()
        run_index.register_run(tmp_path, name, {})
    (tmp_path / "gone").rmdir()
    (tmp_path / "unindexed").mkdir()

    assert run_index.prune_index(tmp_path) == ("gone",)

    assert list(run_index.load_index(tmp_path)) == ["kept"]
    assert (tmp_path / "unindexed").is_dir()
    assert run_index.prune_index(tmp_path) == ()


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        json.dumps({"schema_version": 99, "runs": {}}),
        json.dumps({"schema_version": 1, "runs": []}),
        json.dumps({"schema_version": 1, "runs": {"a": {"directory": "../escape"}}}),
        json.dumps({"schema_version": 1, "runs": {"a": {"directory": ""}}}),
        json.dumps({"schema_version": 1, "runs": {"a": "x"}}),
    ],
)
def test_malformed_index_is_rejected_not_reset(tmp_path: Path, content: str) -> None:
    path = run_index.index_path(tmp_path)
    path.write_text(content, encoding="utf-8")

    with pytest.raises(RunIndexError):
        run_index.prune_index(tmp_path)

    assert path.read_text(encoding="utf-8") == content
