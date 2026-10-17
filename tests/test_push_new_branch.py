# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for automatic pushes of newly checked-out branches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from scripts import push_new_branch

if TYPE_CHECKING:
    from collections.abc import Iterator

    import pytest


@dataclass(frozen=True, slots=True)
class _GitResult:
    """Captured result surface returned by the mocked Git commands."""

    returncode: int
    stdout: str = ""
    stderr: str = ""


def _result(
    *,
    returncode: int = 0,
    stdout: str = "",
    stderr: str = "",
) -> _GitResult:
    """Build a captured result for a fake Git invocation."""
    return _GitResult(returncode, stdout, stderr)


def test_pushes_current_branch_when_upstream_is_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """Push and set tracking for a local branch without an upstream."""
    calls: list[tuple[str, ...]] = []
    results: Iterator[_GitResult] = iter(
        [
            _result(stdout="feature\n"),
            _result(stdout="\n"),
            _result(stdout="https://example.invalid/repo.git\n"),
            _result(),
        ]
    )

    def fake_git(*arguments: str) -> _GitResult:
        calls.append(arguments)
        return next(results)

    monkeypatch.setattr(push_new_branch, "_git", fake_git)

    assert push_new_branch.main() == 0
    assert calls[-1] == ("push", "--set-upstream", "origin", "HEAD")


def test_skips_push_when_branch_already_has_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Leave branches with an existing upstream untouched."""
    calls: list[tuple[str, ...]] = []
    results: Iterator[_GitResult] = iter(
        [
            _result(stdout="feature\n"),
            _result(stdout="refs/remotes/origin/feature\n"),
        ]
    )

    def fake_git(*arguments: str) -> _GitResult:
        calls.append(arguments)
        return next(results)

    monkeypatch.setattr(push_new_branch, "_git", fake_git)

    assert push_new_branch.main() == 0
    assert len(calls) == 2


def test_skips_detached_head(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do nothing when checkout leaves HEAD detached."""

    def fake_git(*_arguments: str) -> _GitResult:
        return _result(returncode=1)

    monkeypatch.setattr(push_new_branch, "_git", fake_git)

    assert push_new_branch.main() == 0


def test_reports_missing_origin_without_trying_to_push(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Return Git's error when the repository has no origin remote."""
    calls: list[tuple[str, ...]] = []
    results: Iterator[_GitResult] = iter(
        [
            _result(stdout="feature\n"),
            _result(),
            _result(returncode=2),
        ]
    )

    def fake_git(*arguments: str) -> _GitResult:
        calls.append(arguments)
        return next(results)

    monkeypatch.setattr(push_new_branch, "_git", fake_git)

    assert push_new_branch.main() == 2
    assert "remote 'origin' is not configured" in caplog.text
    assert calls[-1] == ("remote", "get-url", "origin")
