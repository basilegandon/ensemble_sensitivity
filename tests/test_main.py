# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the main module."""

from __future__ import annotations

import logging
from importlib import import_module
from pathlib import Path
from unittest.mock import Mock

import pytest

from ensemble_sensitivity.main import configure_logging, main, parse_args

main_module = import_module("ensemble_sensitivity.main")


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


def test_main_fetch_dispatches_retrieval_and_logs_result(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    run_dir = Path("retrieved-run")
    retrieve = Mock(return_value=run_dir)
    monkeypatch.setattr(main_module, "retrieve_latest_complete_run", retrieve)

    with caplog.at_level(logging.INFO):
        main(["fetch", "--lead-time", "48", "--step", "12"])

    options = retrieve.call_args.args[0]
    assert options.target_lead_hours == 48
    assert options.step_hours == 12
    assert options.output_dir == Path("output")
    assert options.env_file == Path(".env")
    assert options.show_progress is True
    assert "Raw GRIB fields saved to" in caplog.text
    assert "raw_fetch" in caplog.text


def test_fetch_needs_no_argument_and_accepts_short_options() -> None:
    defaults = parse_args(["fetch"])
    assert (defaults.lead_time, defaults.step) == (24, 24)
    assert (defaults.verbose, defaults.quiet) == (False, False)

    short = parse_args(["fetch", "-l", "72", "-s", "6", "-q"])
    assert (short.lead_time, short.step, short.quiet) == (72, 6, True)


def test_verbosity_flags_work_before_and_after_the_subcommand() -> None:
    assert parse_args(["-v", "fetch"]).verbose is True
    assert parse_args(["fetch", "-v"]).verbose is True
    assert parse_args(["fetch", "--quiet"]).quiet is True
    with pytest.raises(SystemExit):
        parse_args(["fetch", "-v", "-q"])
    for conflicting in (["-v", "fetch", "-q"], ["-q", "fetch", "-v"]):
        with pytest.raises(SystemExit):
            parse_args(conflicting)


def test_quiet_fetch_hides_progress_and_info_logs(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    retrieve = Mock(return_value=Path("retrieved-run"))
    monkeypatch.setattr(main_module, "retrieve_latest_complete_run", retrieve)

    with caplog.at_level(logging.ERROR):
        main(["fetch", "-q"])

    assert retrieve.call_args.args[0].show_progress is False
    assert logging.getLogger().level == logging.ERROR
    assert not caplog.text


def test_quiet_plot_disables_progress(monkeypatch: pytest.MonkeyPatch) -> None:
    render = Mock(return_value=(Path("map.png"),))
    monkeypatch.setattr(main_module, "render_quicklooks", render)

    main(["plot", "--run-dir", "run", "--lead-hours", "24", "-q"])
    assert render.call_args.kwargs["show_progress"] is False
    main(["plot", "--run-dir", "run", "--lead-hours", "24"])
    assert render.call_args.kwargs["show_progress"] is True


@pytest.mark.parametrize(
    ("verbose", "quiet", "expected"),
    [(False, False, logging.INFO), (True, False, logging.DEBUG), (False, True, logging.ERROR)],
)
def test_configure_logging_levels(*, verbose: bool, quiet: bool, expected: int) -> None:
    configure_logging(verbose=verbose, quiet=quiet)

    assert logging.getLogger().level == expected


def test_main_fetch_reports_retrieval_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "retrieve_latest_complete_run",
        Mock(side_effect=main_module.RetrievalError("API unavailable")),
    )

    with pytest.raises(SystemExit, match="1"):
        main(["fetch"])


def test_main_plot_dispatches_rendering_and_logs_count(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    outputs = (Path("first.png"), Path("second.png"))
    render = Mock(return_value=outputs)
    monkeypatch.setattr(main_module, "render_quicklooks", render)

    with caplog.at_level(logging.INFO):
        main(
            [
                "plot",
                "--run-dir",
                "run",
                "--lead-hours",
                "24,48",
                "--members",
                "0,1",
                "--output-dir",
                "maps",
            ]
        )

    assert render.call_args.kwargs == {
        "lead_hours": "24,48",
        "members": "0,1",
        "output_dir": Path("maps"),
        "output_path": None,
        "show_progress": True,
    }
    assert "Saved 2 quicklook map(s)" in caplog.text


def test_main_plot_reports_invalid_selections(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        main_module,
        "render_quicklooks",
        Mock(side_effect=ValueError("invalid selection")),
    )

    with pytest.raises(SystemExit, match="1"):
        main(["plot", "--run-dir", "run", "--lead-hours", "invalid"])
