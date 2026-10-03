---
name: ensemble-sensitivity
description: Implement or review ensemble-sensitivity analysis, forecast-data ingestion, statistical calculations, scientific validation, or related pipeline work in this repository.
---

# Ensemble sensitivity work

Before changing code:

1. Read the relevant README sections. Treat the README as the scientific and product specification; distinguish decisions (✅), hypotheses (🔶), and questions to verify (❓). Do not invent answers for unresolved data or methodology questions.
2. Inspect the existing code and tests. For implementation style, use `src/ensemble_sensitivity/pipeline_checkpoint/pipeline_checkpoint.py` and `tests/test_checkpoint.py` as local examples of typed APIs, focused helpers, logging, sync/async support, and tests—not as a reason to copy unrelated complexity.
3. Keep ingestion, decoding, spatial selection, statistics, output, and orchestration responsibilities separate when those components are introduced. Follow the README's proposed architecture only as the feature requires; do not build speculative modules or install large binary dependencies before confirming the need.

## Scientific and runtime correctness

- Preserve the definitions and invariants in the README. In particular, when implementing the documented target aggregation and sensitivity map, test the weighted-mean identity against a direct per-cell calculation on synthetic data.
- Handle small or zero variance, empty/out-of-grid zones, missing members, invalid grids/units, and non-finite values explicitly according to the specification. Never turn invalid scientific input into a plausible-looking result.
- Make randomization reproducible and record the seed and relevant parameters when adding permutation analysis. Apply the documented multiple-comparison method rather than interpreting pointwise thresholds as map-wide significance.
- Keep the forecast pipeline memory-bounded and process one forecast time at a time when working with full-grid ensemble fields. Preserve laziness and avoid accidental full materialization.
- Use `logging` for operational diagnostics. Fail loudly for failed data retrieval, decoding, or scientific computation. Only optional checkpoint writes use best-effort failure handling, as documented in the checkpoint module.
- Prefer standard-library functionality and existing dependencies. Propose a dependency only when it materially improves the implementation and explain why.

## Tests and verification

- Add focused tests for the changed behavior, with small synthetic fixtures rather than external forecast downloads.
- Test numerical invariants, boundary cases, and invalid-input behavior; use fixed seeds for stochastic tests.
- Run the relevant Ruff, strict mypy, and pytest checks using the locked `uv` environment. Report checks that could not be run.
