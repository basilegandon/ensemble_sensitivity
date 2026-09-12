# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generic automatic and identifiable DataFrame checkpoint pipeline."""

from __future__ import annotations

import functools
import inspect
import itertools
from contextvars import ContextVar, Token
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, overload, override

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Callable
    from types import TracebackType


@dataclass(frozen=True, slots=True)
class _CheckpointConfig:
    """Configuration for a checkpointed function call."""

    label: str
    track_inputs: bool


def _checkpoint_call[**P, R](
    func: Callable[P, R],
    config: _CheckpointConfig,
    mode: CheckpointMode,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Execute a function and checkpoint its DataFrame inputs and outputs.

    Args:
        func: The function to execute.
        config: Checkpoint configuration.
        mode: The current checkpoint mode.
        args: Positional arguments to pass to the function.
        kwargs: Keyword arguments to pass to the function.

    Returns:
        The result returned by the function.

    """
    signature = inspect.signature(func)
    bound = signature.bind_partial(*args, **kwargs).arguments

    if config.track_inputs:
        _checkpoint_inputs(bound, label=config.label, mode=mode)

    result = func(*args, **kwargs)
    _checkpoint_outputs(result, bound, label=config.label, mode=mode)

    return result


_current_mode: ContextVar[CheckpointMode | None] = ContextVar(
    "checkpoint_mode",
    default=None,
)


def _sanitize_label(label: str) -> str:
    """Return a filesystem-safe checkpoint label.

    Args:
        label: The original label.

    Returns:
        str: The sanitized label.

    """
    invalid_characters = '<>:"/\\|?*'
    return "".join("_" if character in invalid_characters else character for character in label)


class CheckpointMode:
    """Context manager enabling DataFrame checkpoints."""

    def __init__(
        self,
        base_dir: str | Path = "./checkpoints",
        run_id: str | None = None,
    ) -> None:
        """Configure the checkpoint directory and run identifier.

        Args:
            base_dir: The base directory for checkpoints.
            run_id: An optional identifier for the current run. If None, a timestamp-based
                identifier is generated.

        """
        self.base_dir = Path(base_dir)
        self.run_id = run_id or datetime.now(tz=UTC).strftime("run_%Y%m%d_%H%M%S")
        self.run_dir = self.base_dir / self.run_id
        self._step_counter = itertools.count(1)
        self._token: Token[CheckpointMode | None] | None = None

    def __enter__(self) -> Self:
        """Enable checkpoint mode.

        Returns:
            Self: The context manager instance.

        """
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self._token = _current_mode.set(self)
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        """Restore the previous checkpoint context.

        Args:
            exc_type: The exception type, if any.
            exc: The exception instance, if any.
            tb: The traceback, if any.


        """
        if self._token is None:
            return

        _current_mode.reset(self._token)
        self._token = None

    def next_step(self) -> int:
        """Return the next checkpoint step number.

        Returns:
            int: The next step number.

        """
        return next(self._step_counter)

    @staticmethod
    def is_active() -> bool:
        """Return whether checkpoint mode is active.

        Returns:
            bool: True if checkpoint mode is active, False otherwise.

        """
        return _current_mode.get() is not None


def _dump(
    df: pl.DataFrame,
    label: str,
    *,
    mode: CheckpointMode,
) -> Path:
    """Dump a DataFrame to a uniquely numbered Parquet file.

    Args:
        df: The DataFrame to dump.
        label: A label to include in the filename.
        mode: The current checkpoint mode.

    Returns:
        Path: The path to the dumped Parquet file.

    """
    idx = mode.next_step()
    path = mode.run_dir / f"{idx:04d}_{label}.parquet"
    df.write_parquet(path)
    return path


def _as_df(value: object) -> pl.DataFrame | None:
    """Return the DataFrame represented by a value, if any.

    Args:
        value: The value to check.

    Returns:
        pl.DataFrame | None: The DataFrame if the value is a DataFrame or has a 'df' attribute that
        is a DataFrame, otherwise None.

    """
    if isinstance(value, pl.DataFrame):
        return value

    maybe = getattr(value, "df", None)
    return maybe if isinstance(maybe, pl.DataFrame) else None


def _checkpoint_inputs(
    bound: dict[str, object],
    *,
    label: str,
    mode: CheckpointMode,
) -> None:
    """Checkpoint DataFrame arguments.

    Args:
        bound: A dictionary of parameter names to argument values.
        label: A label to include in the checkpoint filenames.
        mode: The current checkpoint mode.

    """
    for parameter_name, value in bound.items():
        df = _as_df(value)
        if df is not None:
            _dump(df, f"{label}.in.{parameter_name}", mode=mode)


def _checkpoint_mutated_state(
    bound: dict[str, object],
    *,
    label: str,
    mode: CheckpointMode,
) -> None:
    """Checkpoint DataFrames contained in mutated arguments.

    Args:
        bound: A dictionary of parameter names to argument values.
        label: A label to include in the checkpoint filenames.
        mode: The current checkpoint mode.

    """
    for parameter_name, value in bound.items():
        df = _as_df(value)
        if df is not None:
            _dump(df, f"{label}.out.{parameter_name}", mode=mode)


def _checkpoint_dataframe_outputs(
    outputs: tuple[object, ...], *, label: str, mode: CheckpointMode
) -> None:
    """Checkpoint DataFrame outputs.

    Args:
        outputs: A tuple of output values from a function.
        label: A label to include in the checkpoint filenames.
        mode: The current checkpoint mode.

    """
    multiple = len(outputs) > 1

    for index, value in enumerate(outputs):
        if isinstance(value, pl.DataFrame):
            suffix = f"out{index}" if multiple else "out"
            _dump(value, f"{label}.{suffix}", mode=mode)


def _checkpoint_outputs(
    result: object, bound: dict[str, object], *, label: str, mode: CheckpointMode
) -> None:
    """Checkpoint the result of a decorated function.

    Args:
        result: The result returned by the decorated function.
        bound: A dictionary of parameter names to argument values.
        label: A label to include in the checkpoint filenames.
        mode: The current checkpoint mode.


    """
    if result is None:
        _checkpoint_mutated_state(bound, label=label, mode=mode)
        return

    outputs: tuple[object, ...] = (
        cast("tuple[object, ...]", result) if isinstance(result, tuple) else (result,)
    )
    _checkpoint_dataframe_outputs(outputs, label=label, mode=mode)


@overload
def checkpoint[**P, R](
    func: Callable[P, R],
    *,
    name: str | None = None,
    track_inputs: bool = False,
) -> Callable[P, R]: ...


@overload
def checkpoint[**P, R](
    func: None = None,
    *,
    name: str | None = None,
    track_inputs: bool = False,
) -> Callable[[Callable[P, R]], Callable[P, R]]: ...


def checkpoint[**P, R](
    func: Callable[P, R] | None = None,
    *,
    name: str | None = None,
    track_inputs: bool = False,
) -> Callable[P, R] | Callable[[Callable[P, R]], Callable[P, R]]:
    """Decorate a function with automatic DataFrame checkpoints.

    Args:
        func: The function to decorate.
        name: An optional label to use for checkpoint filenames. If None, the function's qualified
            name is used.
        track_inputs: Whether to checkpoint DataFrame inputs to the function.

    Returns:
        The decorated function.

    """

    def decorator(f: Callable[P, R]) -> Callable[P, R]:
        label = _sanitize_label(name or f.__name__)
        config = _CheckpointConfig(
            label=label,
            track_inputs=track_inputs,
        )

        @functools.wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            mode = _current_mode.get()

            if mode is None:
                return f(*args, **kwargs)

            return _checkpoint_call(
                f,
                config,
                mode,
                *args,
                **kwargs,
            )

        return wrapper

    return decorator(func) if func is not None else decorator


class CheckpointedPipeline:
    """Automatically checkpoint public methods of subclasses."""

    @override
    def __init_subclass__(cls, **kwargs: object) -> None:
        """Decorate public callable attributes of the subclass."""
        super().__init_subclass__(**kwargs)

        for attr_name, attr_value in vars(cls).items():
            if attr_name.startswith("_") or not callable(attr_value):
                continue

            decorated = checkpoint(
                attr_value,
                name=f"{cls.__name__}.{attr_name}",
            )
            setattr(cls, attr_name, decorated)
