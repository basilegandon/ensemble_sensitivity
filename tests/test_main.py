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


def test_parse_plot_args_supports_member_and_lead_lists() -> None:
    args = parse_args(
        [
            "plot",
            "--run-dir",
            "run",
            "--lead-hours",
            "24,48",
            "--members",
            "0,2,34",
        ]
    )
    assert args.lead_hours == "24,48"
    assert args.members == "0,2,34"


def test_parse_plot_args_supports_all_members_and_leads() -> None:
    args = parse_args(["plot", "--run-dir", "run", "--lead-hours", "*", "--members", "*"])
    assert args.lead_hours == "*"
    assert args.members == "*"


def test_configure_logging_verbose(monkeypatch: pytest.MonkeyPatch) -> None:
    """Test that verbose mode updates the root logger level and initializes logging once."""
    calls: list[int] = []
    levels: list[int] = []

    class FakeLogger:
        def __init__(self) -> None:
            self.handlers: list[object] = []
            self.manager = type("Manager", (), {"loggerDict": {}})()

        @staticmethod
        def setLevel(level: int) -> None:  # ruff: ignore[invalid-function-name]
            levels.append(level)

        def addHandler(self, handler: object) -> None:  # ruff: ignore[invalid-function-name]
            self.handlers.append(handler)

        def removeHandler(self, handler: object) -> None:  # ruff: ignore[invalid-function-name]
            if handler in self.handlers:
                self.handlers.remove(handler)

    def fake_basic_config(*, level: int) -> None:
        calls.append(level)

    def fake_get_logger() -> FakeLogger:
        return FakeLogger()

    monkeypatch.setattr(logging, "getLogger", fake_get_logger)
    monkeypatch.setattr(logging, "basicConfig", fake_basic_config)

    configure_logging(verbose=True)

    assert levels == [logging.DEBUG]
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
