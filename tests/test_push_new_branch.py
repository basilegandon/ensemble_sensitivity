# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for automatic pushes of newly checked-out branches."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING
from unittest.mock import Mock

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


def test_reports_branch_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Propagate Git failures other than a detached HEAD result."""
    monkeypatch.setattr(
        push_new_branch,
        "_git",
        lambda *_arguments: _result(returncode=128, stderr="fatal: not a git repository"),
    )

    assert push_new_branch.main() == 128
    assert "fatal: not a git repository" in caplog.text


def test_propagates_missing_git_during_branch_lookup(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Do not mistake a missing Git executable for a detached HEAD."""
    monkeypatch.setattr(push_new_branch, "_git", lambda *_arguments: _result(returncode=127))

    assert push_new_branch.main() == 127
    assert "Git exited with status 127" in caplog.text


def test_skips_file_only_checkout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Do not invoke Git for a pre-commit file-only checkout event."""
    fake_git = Mock(side_effect=AssertionError("Git should not be invoked"))
    monkeypatch.setattr(push_new_branch, "_git", fake_git)
    monkeypatch.setenv("PRE_COMMIT_CHECKOUT_TYPE", "0")

    assert push_new_branch.main() == 0
    fake_git.assert_not_called()


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


def test_reports_upstream_lookup_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Stop with an actionable error when Git cannot inspect the upstream."""
    results: Iterator[_GitResult] = iter([_result(stdout="feature\n"), _result(returncode=3)])
    monkeypatch.setattr(push_new_branch, "_git", lambda *_arguments: next(results))

    assert push_new_branch.main() == 3
    assert "Unable to determine the upstream" in caplog.text


def test_reports_push_failure(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Propagate a remote push failure and include Git's diagnostic."""
    results: Iterator[_GitResult] = iter(
        [
            _result(stdout="feature\n"),
            _result(),
            _result(stdout="https://example.invalid/repo.git\n"),
            _result(returncode=1, stderr="permission denied"),
        ]
    )
    monkeypatch.setattr(push_new_branch, "_git", lambda *_arguments: next(results))

    assert push_new_branch.main() == 1
    assert "permission denied" in caplog.text


def test_git_command_uses_resolved_executable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Invoke Git without a shell using the resolved executable path."""
    fake_run = Mock(
        return_value=push_new_branch.subprocess.CompletedProcess(
            ["git", "status"],
            0,
            "clean",
            "",
        )
    )
    monkeypatch.setattr(push_new_branch, "GIT_EXECUTABLE", "git")
    monkeypatch.setattr(push_new_branch.subprocess, "run", fake_run)

    result = push_new_branch._git("status")

    assert result.stdout == "clean"
    fake_run.assert_called_once_with(
        ["git", "status"],
        check=False,
        capture_output=True,
        text=True,
    )


def test_git_command_reports_missing_executable(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Return a failing process result when Git cannot be found."""
    monkeypatch.setattr(push_new_branch, "GIT_EXECUTABLE", None)

    result = push_new_branch._git("status")

    assert result.returncode == 127
    assert "Git is not available on PATH" in caplog.text
