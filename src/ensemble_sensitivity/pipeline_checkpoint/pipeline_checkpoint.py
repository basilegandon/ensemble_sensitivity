# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Generic automatic and identifiable DataFrame checkpoint pipeline."""

from __future__ import annotations

import asyncio
import functools
import inspect
import itertools
import json
import logging
import threading
from contextvars import ContextVar, Token
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Self, cast, overload, override

import polars as pl

if TYPE_CHECKING:
    from collections.abc import Callable, Coroutine, Iterator
    from types import TracebackType
    from typing import Any, Literal

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _CheckpointConfig:
    """Configuration for a checkpointed function call."""

    label: str
    track_inputs: bool


@dataclass(frozen=True, slots=True)
class ManifestEntry:
    """One row of a run's checkpoint manifest (one line of ``manifest.jsonl``)."""

    step: int
    label: str
    path: str | None
    status: Literal["ok", "error"]
    error: str | None
    timestamp: str

    def to_json(self) -> str:
        """Serialize this entry as a single JSON line.

        Returns:
            str: The JSON-encoded entry, without trailing newline.

        """
        return json.dumps(asdict(self))


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
            run_id: An optional identifier for the current run. If None, a
                timestamp-based identifier is generated.

        """
        self.base_dir = Path(base_dir)
        self.run_id = run_id or datetime.now(tz=UTC).strftime("run_%Y%m%d_%H%M%S_%f")
        self.run_dir = self.base_dir / self.run_id
        self.manifest_path = self.run_dir / "manifest.jsonl"
        self._step_counter = itertools.count(1)
        self._manifest_lock = threading.Lock()
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
            int: The next step number. ``itertools.count.__next__`` is
            implemented in C and is atomic under the GIL, so this is safe
            to call concurrently from several threads (e.g. via
            ``asyncio.to_thread``) without an explicit lock.

        """
        return next(self._step_counter)

    def record(self, entry: ManifestEntry) -> None:
        """Append one entry to this run's manifest, best-effort.

        Args:
            entry: The manifest entry to append.

        """
        try:
            with self._manifest_lock, self.manifest_path.open("a", encoding="utf-8") as handle:
                handle.write(entry.to_json() + "\n")
        except OSError:
            logger.warning(
                "Impossible d'écrire dans le manifest %s", self.manifest_path, exc_info=True
            )

    @staticmethod
    def is_active() -> bool:
        """Return whether checkpoint mode is active.

        Returns:
            bool: True if checkpoint mode is active, False otherwise.

        """
        return _current_mode.get() is not None


def _dump(frame: pl.DataFrame | pl.LazyFrame, label: str, *, mode: CheckpointMode) -> Path | None:
    """Persist one frame as a uniquely numbered Parquet file, best-effort.

    A DataFrame is written eagerly (``write_parquet``); a LazyFrame is
    streamed directly to disk (``sink_parquet``) without being collected
    into memory first, using the best engine Polars can select for the
    query (``engine="auto"``: the engine set by
    ``pl.Config.set_engine_affinity``/the ``POLARS_ENGINE_AFFINITY``
    environment variable, falling back to the streaming engine).

    This function never raises: a failure is logged with its precise
    cause and recorded in the run's manifest, but never interrupts the
    pipeline being checkpointed — checkpointing is an observability
    side effect, not part of the actual computation.

    Args:
        frame: The DataFrame or LazyFrame to persist.
        label: A label to include in the filename.
        mode: The current checkpoint mode.

    Returns:
        Path | None: The path written to, or None if the write failed.

    """
    step = mode.next_step()
    path = mode.run_dir / f"{step:04d}_{label}.parquet"
    timestamp = datetime.now(tz=UTC).isoformat()

    try:
        if isinstance(frame, pl.LazyFrame):
            frame.sink_parquet(path, engine="auto")
        else:
            frame.write_parquet(path)
    except Exception as exc:  # ruff: ignore[blind-except] - un échec de checkpoint ne doit jamais casser le pipeline
        reason = f"{type(exc).__name__}: {exc}"
        logger.warning("Checkpoint %r (étape %d) échoué : %s", label, step, reason)
        mode.record(
            ManifestEntry(
                step=step,
                label=label,
                path=None,
                status="error",
                error=reason,
                timestamp=timestamp,
            ),
        )
        return None

    mode.record(
        ManifestEntry(
            step=step,
            label=label,
            path=str(path),
            status="ok",
            error=None,
            timestamp=timestamp,
        ),
    )
    return path


def _as_frame(value: object) -> pl.DataFrame | pl.LazyFrame | None:
    """Return the Polars frame represented by a value, if any.

    Args:
        value: The value to check.

    Returns:
        pl.DataFrame | pl.LazyFrame | None: The frame if the value is one,
        or has a 'df' attribute that is one, otherwise None.

    """
    if isinstance(value, (pl.DataFrame, pl.LazyFrame)):
        return value

    maybe = getattr(value, "df", None)
    return maybe if isinstance(maybe, (pl.DataFrame, pl.LazyFrame)) else None


def _iter_input_frames(
    bound: dict[str, object],
) -> Iterator[tuple[str, pl.DataFrame | pl.LazyFrame]]:
    """Yield each (suffix, frame) pair found among a function's bound arguments.

    Args:
        bound: A dictionary of parameter names to argument values.

    Yields:
        tuple[str, pl.DataFrame | pl.LazyFrame]: A filename suffix and the
        frame it identifies.

    """
    for parameter_name, value in bound.items():
        frame = _as_frame(value)
        if frame is not None:
            yield f"in.{parameter_name}", frame


def _iter_output_frames(
    result: object,
    bound: dict[str, object],
) -> Iterator[tuple[str, pl.DataFrame | pl.LazyFrame]]:
    """Yield each (suffix, frame) pair found in a function's result.

    Args:
        result: The value returned by the decorated function.
        bound: A dictionary of parameter names to argument values, used
            when `result` is None (mutated-state convention).

    Yields:
        tuple[str, pl.DataFrame | pl.LazyFrame]: A filename suffix and the
        frame it identifies.

    """
    if result is None:
        # 0 sortie -> état muté en place : on relit chaque argument, dont
        # l'état reflète maintenant la sortie de l'appel qui vient de finir.
        for parameter_name, value in bound.items():
            frame = _as_frame(value)
            if frame is not None:
                yield f"out.{parameter_name}", frame
        return

    outputs = tuple(result) if isinstance(result, (tuple, list)) else (result,)
    multiple = len(outputs) > 1
    for index, value in enumerate(outputs):
        if isinstance(value, (pl.DataFrame, pl.LazyFrame)):
            yield (f"out{index}" if multiple else "out"), value


def _checkpoint_call[**P, R](
    func: Callable[P, R],
    config: _CheckpointConfig,
    mode: CheckpointMode,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Execute a synchronous function and checkpoint its frames.

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
        for suffix, frame in _iter_input_frames(bound):
            _dump(frame, f"{config.label}.{suffix}", mode=mode)

    result = func(*args, **kwargs)

    for suffix, frame in _iter_output_frames(result, bound):
        _dump(frame, f"{config.label}.{suffix}", mode=mode)

    return result


async def _checkpoint_call_async[**P, R](
    func: Callable[P, Coroutine[Any, Any, R]],
    config: _CheckpointConfig,
    mode: CheckpointMode,
    *args: P.args,
    **kwargs: P.kwargs,
) -> R:
    """Execute an async function and checkpoint its frames.

    Identical to `_checkpoint_call`, except the decorated function is
    awaited, and each Parquet write runs in a worker thread
    (`asyncio.to_thread`) so it never blocks the event loop.

    Args:
        func: The coroutine function to execute.
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
        for suffix, frame in _iter_input_frames(bound):
            await asyncio.to_thread(_dump, frame, f"{config.label}.{suffix}", mode=mode)

    result = await func(*args, **kwargs)

    for suffix, frame in _iter_output_frames(result, bound):
        await asyncio.to_thread(_dump, frame, f"{config.label}.{suffix}", mode=mode)

    return result


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
    """Decorate a function with automatic DataFrame/LazyFrame checkpoints.

    Works transparently on both regular and ``async def`` functions.
    Outside an active `CheckpointMode`, the decorated function runs
    completely unmodified — for a LazyFrame in particular, this means no
    collection ever happens implicitly; it stays exactly as lazy as the
    function made it, unless the function itself calls `.collect()`.

    Args:
        func: The function to decorate.
        name: An optional label for checkpoint filenames. If None, the
            function's name is used.
        track_inputs: Whether to also checkpoint DataFrame/LazyFrame
            arguments, in addition to the output(s).

    Returns:
        The decorated function.

    """

    def decorator(f: Callable[P, R]) -> Callable[P, R]:
        label = _sanitize_label(name or f.__name__)
        config = _CheckpointConfig(label=label, track_inputs=track_inputs)

        if inspect.iscoroutinefunction(f):
            # `f` est ici une coroutine function : Callable[P, Coroutine[Any, Any, R]].
            # Le vérificateur de types ne peut pas déduire cette restriction d'un
            # simple test à l'exécution (`iscoroutinefunction`) : le `cast` rend
            # explicite ce que la vérification runtime vient de garantir.
            async_f = cast("Callable[P, Coroutine[Any, Any, R]]", f)

            @functools.wraps(f)
            async def async_wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
                mode = _current_mode.get()
                if mode is None:
                    return await async_f(*args, **kwargs)
                return await _checkpoint_call_async(async_f, config, mode, *args, **kwargs)

            return cast("Callable[P, R]", async_wrapper)

        @functools.wraps(f)
        def wrapper(*args: P.args, **kwargs: P.kwargs) -> R:
            mode = _current_mode.get()
            if mode is None:
                return f(*args, **kwargs)
            return _checkpoint_call(f, config, mode, *args, **kwargs)

        return wrapper

    return decorator(func) if func is not None else decorator


class CheckpointedPipeline:
    """Automatically checkpoint public methods of subclasses (sync or async)."""

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
