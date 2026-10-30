# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Push a checked-out local branch when it does not have an upstream."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys

LOGGER = logging.getLogger(__name__)
GIT_EXECUTABLE = shutil.which("git")


def _git(*arguments: str) -> subprocess.CompletedProcess[str]:
    """Run Git and capture its output for actionable hook diagnostics.

    Returns:
        The process result.

    """
    if GIT_EXECUTABLE is None:
        LOGGER.error("Cannot push a new branch: Git is not available on PATH.")
        return subprocess.CompletedProcess(["git", *arguments], 127, "", "")
    return subprocess.run(
        [GIT_EXECUTABLE, *arguments],
        check=False,
        capture_output=True,
        text=True,
    )


def main() -> int:
    """Push the current branch to origin if it has no upstream.

    Returns:
        The Git exit status, or zero when no push is necessary.

    """
    if os.environ.get("PRE_COMMIT_CHECKOUT_TYPE") == "0":
        return 0

    branch, status = _current_branch()
    if branch is None:
        return status

    has_upstream, status = _branch_upstream_status(branch)
    if status != 0:
        return status
    if has_upstream:
        return 0

    return _push_to_origin(branch)


def _current_branch() -> tuple[str | None, int]:
    """Return the checked-out branch, or a status when it cannot be resolved.

    Returns:
        Branch name and status code; None indicates no symbolic branch.

    """
    branch_result = _git("symbolic-ref", "--quiet", "--short", "HEAD")
    if branch_result.returncode == 0:
        return branch_result.stdout.strip(), 0
    if branch_result.returncode != 1:
        LOGGER.error(
            "Unable to determine the current branch: %s",
            branch_result.stderr.strip() or f"Git exited with status {branch_result.returncode}.",
        )
    return None, 0 if branch_result.returncode == 1 else branch_result.returncode


def _branch_upstream_status(branch: str) -> tuple[bool, int]:
    """Check whether a local branch already has a configured upstream.

    Returns:
        Whether an upstream exists and the Git status code.

    """
    upstream_result = _git(
        "for-each-ref",
        "--format=%(upstream)",
        f"refs/heads/{branch}",
    )
    if upstream_result.returncode != 0:
        LOGGER.error("Unable to determine the upstream for branch %s.", branch)
        return False, upstream_result.returncode
    if upstream_result.stdout.strip():
        return True, 0
    return False, 0


def _push_to_origin(branch: str) -> int:
    """Push a branch to origin and set its upstream when successful.

    Returns:
        The Git exit status.

    """
    remote_result = _git("remote", "get-url", "origin")
    if remote_result.returncode != 0:
        LOGGER.error("Cannot push branch %s: remote 'origin' is not configured.", branch)
        return remote_result.returncode

    LOGGER.info("Pushing branch %s to origin and setting its upstream.", branch)
    push_result = _git("push", "--set-upstream", "origin", "HEAD")
    if push_result.returncode != 0:
        LOGGER.error(
            "Unable to push branch %s to origin: %s",
            branch,
            push_result.stderr.strip(),
        )
    return push_result.returncode


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    sys.exit(main())
