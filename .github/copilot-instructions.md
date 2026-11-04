# Project guidance

This is a Python project for exploratory ensemble sensitivity analysis of PEARP forecasts. The README is the domain specification and records which scientific and data-source details are decided, hypotheses, or still unverified. Read the relevant sections before changing behavior; do not silently turn an open question into a fact.

## Engineering standards

- Match the clarity and rigor of `src/ensemble_sensitivity/pipeline_checkpoint/pipeline_checkpoint.py` and its tests. Reuse the standard library and existing project helpers before adding dependencies or abstractions.
- Keep responsibilities narrow, APIs typed, and errors explicit. Log actionable diagnostics through the standard `logging` module; do not use `print`, suppress computation errors, or add broad exception fallbacks.
- Treat checkpoint persistence as optional observability: its documented best-effort behavior must not be generalized to scientific computation, data ingestion, or validation failures.
- Preserve lazy and bounded-memory behavior where relevant. Do not collect, copy, or materialize large arrays/dataframes without a clear need.
- Add or update focused tests for changed behavior, including edge cases and scientific invariants. Use deterministic inputs/seeds for stochastic calculations.
- Keep changes scoped to the request and follow the existing Python 3.14, `uv`, Ruff, strict ty, pytest, and Bandit configuration in `pyproject.toml`.

## Validation

Use the repository's locked environment and run the smallest applicable checks. The CI quality commands are:

```bash
uv run --locked --no-sync --no-build ruff format --check .
uv run --locked --no-sync --no-build ruff check .
uv run --locked --no-sync --no-build ty check .
uv run --locked --no-sync --no-build vulture --config pyproject.toml
uv run --locked --no-sync --no-build xenon --max-absolute A --max-modules A --max-average A src scripts data_retrieval
uv run --locked --no-sync --no-build bandit -r src
uv run --locked --no-sync --no-build pytest
```

If a check cannot run, state why and do not imply it passed.

## Caveman (token-saving) mode

Sessions in this repository use the global `caveman` skill by default. Load and follow `C:\Users\PC\.agents\skills\caveman\SKILL.md` at session start; the skill stays on until the user says "stop caveman" or "normal mode". Do not copy or edit the skills in the user's home directory; refer to them by name and path.

- Default: `caveman` (`C:\Users\PC\.agents\skills\caveman`).
- On request only: `ultracave` (`/ultracave` or `/caveman ultra`, maximum compression) or `megacave` (`/megacave` or `/caveman wenyan`, Classical Chinese). Both are in `C:\Users\PC\.agents\skills\`. When one is active, apply the compression to visible output and to internal reasoning as well, as far as the host allows.
- Persisted content (code, comments, commits, documentation, issues, PRs, memory) stays normal prose: README and docs remain in French, instructions in English, as the skills themselves specify.
- Related skills in the same folder, used when their own trigger applies: `caveman-commit` (commit messages), `caveman-review` (diff review), `caveman-compress` (compress a memory file; sends its text to the model provider), `caveman-help` (reference card), `caveman-stats` (token usage), `caveman-explore` (read-only repository localization), `caveman-learn`, `caveman-discover`, `caveman-setup`, `caveman-evidence-review`, `caveman-manage` and `caveman-optimize` (Caveman Cloud workflows; use only when explicitly requested).

## Git hooks

After cloning the repository, install its local hooks once:

```bash
uv run --locked --no-sync --no-build pre-commit install
```

This installs the commit checks and a post-checkout hook. When a checked-out
branch has no upstream, the hook immediately pushes it to `origin` and sets
that upstream. Git worktrees sharing this repository use the installed hooks.
Each push triggers the existing CI and SonarQube scan when `SONAR_TOKEN` is
configured.
