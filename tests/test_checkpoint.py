# Copyright (C) 2026 Basile Gandon
# SPDX-License-Identifier: GPL-3.0-or-later
"""Tests for the automatic DataFrame checkpoint pipeline."""

import inspect
from dataclasses import dataclass
from typing import TYPE_CHECKING

import polars as pl
import pytest

from ensemble_sensitivity.pipeline_checkpoint import (
    CheckpointedPipeline,
    CheckpointMode,
    checkpoint,
)

if TYPE_CHECKING:
    from pathlib import Path


def _parquets(directory: Path) -> list[Path]:
    return sorted(directory.rglob("*.parquet"))


def test_checkpoint_is_inactive_by_default() -> None:
    """Test that the checkpoint mode is inactive when not explicitly enabled."""
    assert not CheckpointMode.is_active()


def test_checkpoint_is_noop_when_mode_is_inactive() -> None:
    """Test that the checkpoint decorator does not affect function behavior when inactive."""

    @checkpoint(name="transform")
    def transform(df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(pl.lit(value=True).alias("flag"))

    df = pl.DataFrame({"id": [1, 2, 3]})
    result = transform(df)

    expected = pl.DataFrame(
        {
            "id": [1, 2, 3],
            "flag": [True, True, True],
        }
    )
    assert result.equals(expected)


def test_checkpoint_saves_dataframe_output(tmp_path: Path) -> None:
    """Test that the checkpoint decorator saves the output DataFrame when the mode is active."""

    @checkpoint(name="transform")
    def transform(df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(pl.lit(value=True).alias("flag"))

    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        result = transform(df)

    files = _parquets(tmp_path / "run")

    assert [path.name for path in files] == ["0001_transform.out.parquet"]
    assert pl.read_parquet(files[0]).equals(result)


def test_checkpoint_tracks_inputs_when_requested(tmp_path: Path) -> None:
    """Test that the checkpoint decorator saves the input DataFrame when track_inputs is True."""

    @checkpoint(name="transform", track_inputs=True)
    def transform(df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(pl.lit(value=True).alias("flag"))

    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        transform(df)

    files = _parquets(tmp_path / "run")

    assert [path.name for path in files] == [
        "0001_transform.in.df.parquet",
        "0002_transform.out.parquet",
    ]
    assert pl.read_parquet(files[0]).equals(df)


def test_checkpoint_does_not_track_inputs_by_default(tmp_path: Path) -> None:
    """Test that the checkpoint decorator does not save input DataFrame when track_inputs False."""

    @checkpoint(name="transform")
    def transform(df: pl.DataFrame) -> pl.DataFrame:
        return df

    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        transform(df)

    assert [path.name for path in _parquets(tmp_path / "run")] == [
        "0001_transform.out.parquet",
    ]


def test_checkpoint_saves_multiple_dataframe_outputs(tmp_path: Path) -> None:
    """Test that the checkpoint decorator saves multiple DataFrame outputs."""

    @checkpoint(name="split")
    def split(df: pl.DataFrame) -> tuple[pl.DataFrame, pl.DataFrame]:
        return df.head(1), df.tail(2)

    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        result = split(df)

    files = _parquets(tmp_path / "run")

    assert [path.name for path in files] == [
        "0001_split.out0.parquet",
        "0002_split.out1.parquet",
    ]
    assert pl.read_parquet(files[0]).equals(result[0])
    assert pl.read_parquet(files[1]).equals(result[1])


def test_checkpoint_ignores_non_dataframe_output(tmp_path: Path) -> None:
    """Test that the checkpoint decorator ignores non-DataFrame outputs."""

    @checkpoint(name="scalar")
    def scalar(df: pl.DataFrame) -> int:
        return int(df.height)

    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        assert scalar(df) == 3

    assert not _parquets(tmp_path / "run")


def test_checkpoint_tracks_arguments_when_result_is_none(tmp_path: Path) -> None:
    """Test that the checkpoint decorator tracks arguments even when the function returns None."""

    @checkpoint(name="mutate")
    def mutate(df: pl.DataFrame) -> None:
        _ = df

    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        assert mutate(df) is None

    files = _parquets(tmp_path / "run")

    assert [path.name for path in files] == ["0001_mutate.out.df.parquet"]
    assert pl.read_parquet(files[0]).equals(df)


def test_checkpoint_supports_keyword_arguments(tmp_path: Path) -> None:
    """Test that the checkpoint decorator correctly handles keyword arguments."""

    @checkpoint(name="transform", track_inputs=True)
    def transform(
        df: pl.DataFrame,
        *,
        multiplier: int,
    ) -> pl.DataFrame:
        return df.with_columns(
            (pl.col("value") * multiplier).alias("value"),
        )

    df = pl.DataFrame({"value": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        result = transform(df=df, multiplier=2)

    assert result["value"].to_list() == [2, 4, 6]
    assert [path.name for path in _parquets(tmp_path / "run")] == [
        "0001_transform.in.df.parquet",
        "0002_transform.out.parquet",
    ]


def test_checkpoint_tracks_objects_exposing_dataframe(tmp_path: Path) -> None:
    """Test that the checkpoint decorator tracks objects that expose a DataFrame attribute."""

    @dataclass
    class Container:
        def __init__(self, df: pl.DataFrame) -> None:
            self.df = df

    @checkpoint(name="transform", track_inputs=True)
    def transform(container: Container) -> pl.DataFrame:
        return container.df

    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        result = transform(Container(df))

    files = _parquets(tmp_path / "run")

    assert [path.name for path in files] == [
        "0001_transform.in.container.parquet",
        "0002_transform.out.parquet",
    ]
    assert pl.read_parquet(files[0]).equals(df)
    assert result.equals(df)


def test_checkpoint_mode_creates_run_directory(tmp_path: Path) -> None:
    """Test that the CheckpointMode context manager creates the run directory when activated."""
    run_dir = tmp_path / "checkpoints" / "run"

    with CheckpointMode(tmp_path / "checkpoints", run_id="run"):
        assert CheckpointMode.is_active()
        assert run_dir.is_dir()

    assert not CheckpointMode.is_active()


def test_checkpoint_mode_restores_state_after_exception(
    tmp_path: Path,
) -> None:
    """Test that CheckpointMode restores its state after an exception."""

    def run_checkpoint() -> None:
        msg = "boom"
        with CheckpointMode(tmp_path, run_id="run"):
            assert CheckpointMode.is_active()
            raise ValueError(msg)

    with pytest.raises(ValueError, match="boom"):
        run_checkpoint()

    assert not CheckpointMode.is_active()


def test_checkpoint_mode_restores_nested_state(tmp_path: Path) -> None:
    """Test that the CheckpointMode context manager correctly restores state in nested contexts."""
    with CheckpointMode(tmp_path, run_id="outer"):
        assert CheckpointMode.is_active()

        with CheckpointMode(tmp_path, run_id="inner"):
            assert CheckpointMode.is_active()

        assert CheckpointMode.is_active()

    assert not CheckpointMode.is_active()


def test_checkpoint_supports_bare_decorator(tmp_path: Path) -> None:
    """Test that the checkpoint decorator works without arguments."""

    @checkpoint
    def transform(df: pl.DataFrame) -> pl.DataFrame:
        return df

    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        result = transform(df)

    assert result.equals(df)
    assert [path.name for path in _parquets(tmp_path / "run")] == [
        "0001_transform.out.parquet",
    ]


def test_checkpoint_preserves_function_metadata() -> None:
    """Test that the checkpoint decorator preserves the original function's metadata."""

    @checkpoint(name="custom")
    def transform(df: pl.DataFrame) -> pl.DataFrame:
        """Transform a DataFrame.

        Args:
            df: The input DataFrame.

        Returns:
            The transformed DataFrame.

        """
        return df

    assert transform.__name__ == "transform"
    assert transform.__doc__ == inspect.unwrap(transform).__doc__


def test_checkpointed_pipeline_decorates_public_methods(
    tmp_path: Path,
) -> None:
    """Test that the CheckpointedPipeline class decorates public methods for checkpointing."""

    class Pipeline(CheckpointedPipeline):
        def transform(self, df: pl.DataFrame) -> pl.DataFrame:  # ruff: ignore[no-self-use]
            return df.with_columns(pl.lit(value=True).alias("flag"))

        def _private(self, df: pl.DataFrame) -> pl.DataFrame:  # ruff: ignore[no-self-use]
            return df

    pipeline = Pipeline()
    df = pl.DataFrame({"id": [1, 2, 3]})

    with CheckpointMode(tmp_path, run_id="run"):
        result = pipeline.transform(df)
        private_result = pipeline._private(df)  # ruff: ignore[private-member-access]

    assert result.height == 3
    assert private_result.equals(df)
    assert [path.name for path in _parquets(tmp_path / "run")] == [
        "0001_Pipeline.transform.out.parquet",
    ]


def test_checkpointed_pipeline_leaves_private_methods_untouched() -> None:
    """Test that the CheckpointedPipeline class does not decorate private methods."""

    class Pipeline(CheckpointedPipeline):
        def _private(self, df: pl.DataFrame) -> pl.DataFrame:  # ruff: ignore[no-self-use]
            return df

    assert Pipeline()._private.__name__ == "_private"  # ruff: ignore[private-member-access]


def test_interleaved_pipeline_preserves_execution_order(
    tmp_path: Path,
) -> None:
    """Checkpoint files reflect actual execution order."""

    @checkpoint(name="A3")
    def to_a3(df: pl.DataFrame) -> pl.DataFrame:
        return df.with_columns(pl.lit(value=True).alias("flag"))

    @checkpoint(name="B5", track_inputs=True)
    def to_b5(
        a3: pl.DataFrame,
        b: pl.DataFrame,
    ) -> pl.DataFrame:
        return b.join(
            a3.select(["id", "flag"]),
            on="id",
            how="left",
        )

    @checkpoint(name="A7", track_inputs=True)
    def to_a7(
        a3: pl.DataFrame,
        b5: pl.DataFrame,
    ) -> pl.DataFrame:
        return a3.join(
            b5.select(["id", "value"]),
            on="id",
            how="left",
        )

    a = pl.DataFrame({"id": [1, 2, 3], "x": [10, 20, 30]})
    b = pl.DataFrame({"id": [1, 2, 3], "value": [100, 200, 300]})

    with CheckpointMode(tmp_path, run_id="run"):
        a3 = to_a3(a)
        b5 = to_b5(a3, b)
        a7 = to_a7(a3, b5)

    files = _parquets(tmp_path / "run")

    assert [path.name for path in files] == [
        "0001_A3.out.parquet",
        "0002_B5.in.a3.parquet",
        "0003_B5.in.b.parquet",
        "0004_B5.out.parquet",
        "0005_A7.in.a3.parquet",
        "0006_A7.in.b5.parquet",
        "0007_A7.out.parquet",
    ]

    assert pl.read_parquet(files[-1]).equals(a7)
