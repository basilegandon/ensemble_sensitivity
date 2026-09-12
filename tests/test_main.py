# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the main module."""

import logging
from typing import TYPE_CHECKING

from ensemble_sensitivity.main import configure_logging, main, parse_args

if TYPE_CHECKING:
    import pytest


def test_parse_args_default() -> None:
    """Test argument parsing without arguments."""
    args = parse_args([])

    assert args.verbose is False


def test_parse_args_verbose() -> None:
    """Test argument parsing with verbose mode."""
    args = parse_args(["--verbose"])

    assert args.verbose is True


def test_configure_logging_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that verbose mode configures DEBUG logging."""
    calls: list[int] = []

    def fake_basic_config(*, level: int) -> None:
        calls.append(level)

    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)

    configure_logging(verbose=True)

    assert calls == [logging.DEBUG]


def test_main(caplog: pytest.LogCaptureFixture) -> None:
    """Test the main application."""
    with caplog.at_level(logging.INFO):
        main([])

    assert "Hello from ensemble-sensitivity!" in caplog.text


def test_main_verbose(caplog: pytest.LogCaptureFixture) -> None:
    """Test the main application in verbose mode."""
    with caplog.at_level(logging.DEBUG):
        main(["--verbose"])

    assert "Debugging is enabled." in caplog.text
