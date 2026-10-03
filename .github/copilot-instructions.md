# Project guidance

This is a Python project for exploratory ensemble sensitivity analysis of PEARP forecasts. The README is the domain specification and records which scientific and data-source details are decided, hypotheses, or still unverified. Read the relevant sections before changing behavior; do not silently turn an open question into a fact.

## Engineering standards

- Match the clarity and rigor of `src/ensemble_sensitivity/pipeline_checkpoint/pipeline_checkpoint.py` and its tests. Reuse the standard library and existing project helpers before adding dependencies or abstractions.
- Keep responsibilities narrow, APIs typed, and errors explicit. Log actionable diagnostics through the standard `logging` module; do not use `print`, suppress computation errors, or add broad exception fallbacks.
- Treat checkpoint persistence as optional observability: its documented best-effort behavior must not be generalized to scientific computation, data ingestion, or validation failures.
- Preserve lazy and bounded-memory behavior where relevant. Do not collect, copy, or materialize large arrays/dataframes without a clear need.
- Add or update focused tests for changed behavior, including edge cases and scientific invariants. Use deterministic inputs/seeds for stochastic calculations.
- Keep changes scoped to the request and follow the existing Python 3.14, `uv`, Ruff, strict mypy, pytest, and Bandit configuration in `pyproject.toml`.

## Validation

Use the repository's locked environment and run the smallest applicable checks. The CI quality commands are:

```bash
uv run --locked --no-sync --no-build ruff format --check .
uv run --locked --no-sync --no-build ruff check .
uv run --locked --no-sync --no-build mypy .
uv run --locked --no-sync --no-build bandit -r src
uv run --locked --no-sync --no-build pytest
```

If a check cannot run, state why and do not imply it passed.
