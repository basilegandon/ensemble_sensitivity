# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Global index of versioned runs.

Every run owns a directory named by its UUID under the output root. The index file
``runs_index.json`` at the root lists each run, its directory and its parameters.
Directories deleted by hand are purged from the index at the next launch.
"""

import json
import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

logger = logging.getLogger(__name__)

INDEX_FILENAME = "runs_index.json"
SCHEMA_VERSION = 1


class RunIndexError(ValueError):
    """Raised when the run index is unreadable, malformed or inconsistent."""


def new_run_id() -> str:
    """Create a run identifier.

    Returns:
        A random UUID in canonical lowercase form.

    """
    return str(uuid.uuid4())


def index_path(root: Path) -> Path:
    """Locate the index file of an output root.

    Returns:
        Path of ``runs_index.json`` under ``root``.

    """
    return root / INDEX_FILENAME


def load_index(root: Path) -> dict[str, dict[str, object]]:
    """Read the index of an output root.

    Returns:
        Entries keyed by run UUID; empty when no index exists yet.

    Raises:
        RunIndexError: If the file is not valid JSON of the expected shape.

    """
    path = index_path(root)
    if not path.is_file():
        return {}
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise RunIndexError(f"Run index is not valid JSON: {path}") from error
    return _validated_runs(document, path)


def _validated_runs(document: object, path: Path) -> dict[str, dict[str, object]]:
    """Check the index document structure.

    Returns:
        The validated ``runs`` mapping.

    Raises:
        RunIndexError: If the schema version, mapping or an entry is invalid.

    """
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise RunIndexError(f"Unsupported run index schema (expected {SCHEMA_VERSION}): {path}")
    runs = document.get("runs")
    if not isinstance(runs, dict):
        raise RunIndexError(f"Run index has no 'runs' mapping: {path}")
    for run_id, entry in runs.items():
        _check_entry(run_id, entry, path)
    return cast("dict[str, dict[str, object]]", runs)


def _check_entry(run_id: object, entry: object, path: Path) -> None:
    """Check that an entry names a plain directory directly under the output root.

    Raises:
        RunIndexError: If the entry is not a mapping with a safe ``directory``.

    """
    directory = entry.get("directory") if isinstance(entry, dict) else None
    if not isinstance(directory, str) or not directory or Path(directory).name != directory:
        raise RunIndexError(f"Run index entry {run_id!r} has an invalid directory: {path}")


def _write_index(root: Path, runs: dict[str, dict[str, object]]) -> None:
    """Persist the index atomically."""
    path = index_path(root)
    temporary_path = path.with_suffix(".json.tmp")
    document = {"schema_version": SCHEMA_VERSION, "runs": runs}
    temporary_path.write_text(json.dumps(document, indent=2, sort_keys=True) + "\n", "utf-8")
    temporary_path.replace(path)


def register_run(
    root: Path,
    run_id: str,
    parameters: dict[str, object],
    created_at: datetime | None = None,
) -> None:
    """Add a run whose directory ``root/run_id`` exists.

    Raises:
        RunIndexError: If the run is already indexed or its directory is missing.

    """
    runs = load_index(root)
    if run_id in runs:
        raise RunIndexError(f"Run already indexed: {run_id}")
    if not (root / run_id).is_dir():
        raise RunIndexError(f"Run directory does not exist: {root / run_id}")
    runs[run_id] = {
        "directory": run_id,
        "created_at_utc": (created_at or datetime.now(UTC)).astimezone(UTC).isoformat(),
        "parameters": parameters,
    }
    _write_index(root, runs)


def prune_index(root: Path) -> tuple[str, ...]:
    """Drop entries whose directory no longer exists.

    Directories absent from the index are left untouched.

    Returns:
        UUIDs removed from the index.

    """
    runs = load_index(root)
    missing = tuple(
        run_id for run_id, entry in runs.items() if not (root / str(entry["directory"])).is_dir()
    )
    if not missing:
        return ()
    for run_id in missing:
        del runs[run_id]
        logger.info("Removed run %s from the index: its directory no longer exists", run_id)
    _write_index(root, runs)
    return missing
