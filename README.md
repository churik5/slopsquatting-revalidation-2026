# Pre-registered revalidation of an LLM package-hallucination study — code and aggregate results

This repository is the public research artifact for the revalidation of the preprint
*The Range Shrinks, the Threat Remains: Re-evaluating LLM Package Hallucinations on the
2026 Frontier-Model Cohort* (arXiv:2605.17062). It contains the pre-registration, every
deviation from it, the analysis code, its tests, and the aggregate reports the code
produced. It contains **no package names and no raw data** (see below).

## What is here

| Path | Content |
|---|---|
| `PREREG.md` | Pre-registration of Task 1 (revalidation of every paper number) and Task 2 (registration of hallucinated names after publication), committed before any live registry check |
| `PREREG-deviations.md` | Every deviation (D0–D19), each with the moment it was written relative to the analysis |
| `src/slop_bench/` | The original benchmark pipeline (prompting, extraction, validation, noise filter) |
| `scripts/` | Pre-registration baseline, live registry checks, Task 1 and Task 2 analyses, v4 tables and figures |
| `tests/` | Offline tests (no network) |
| `reports/` | Aggregate outputs: counts, rates, tests, hashes of private lists; `reports/v4/` has the paper tables and figures |
| `githooks/` | The pre-commit hook that blocks name lists from being committed |

The commit history is the history of the private working repository, filtered to the paths
above with `git filter-repo`; author and commit dates are preserved, so the order of
pre-registration, deviations and results can be checked. Commit hashes differ from the
hashes cited inside some files (they refer to the private repository).

## Why there is no data

- **Hallucinated package names are an attack surface.** Most names that the models invented
  are still free to register on PyPI or npm (slopsquatting). Publishing them would hand an
  attacker a ready-made list. Reports therefore contain only counts and SHA-256 hashes of
  sorted name lists, which let anyone holding the private data check the numbers.
- **The generation database (`runs.db`, ~840 MB, 199,840 generations) is not shared**, as
  stated in `PREREG.md` §0. It contains the generated code from which the names come.
- **Registry check records, notes and correspondence are not included.** The raw registry
  responses are keyed by name; the notes contain third-party data.

For the same reason, four package-name strings in the test fixtures of
`tests/test_noise_filter.py` were replaced throughout the history by placeholders
(`@redacted/pkg-a`, `redacted-pkg-b`…`d`), and one word in two rule descriptions of
`src/slop_bench/noise_filter.py` was hyphenated ("built-in"). The tests do not depend on the
specific strings.

## How to reproduce

The analysis is deterministic (seed 42) and offline after the live checks. With access to
the private data (`data/runs.db`, `data/private/`, `data/reference/`, master lists, Spracklen
artifact under `ext/`), run from the repository root:

```sh
python scripts/prereg_baseline.py                 # offline baseline, before any live check
python scripts/registry_check.py --round 1        # live checks (PyPI, npm, deps.dev, OSV), <= 1 req/s
python scripts/registry_check.py --round 2        # >= 12 h after round 1 ended
python scripts/task1_analysis.py                  # Task 1, stage A -> reports/numbers.json, task1_report.md
python scripts/npm_placeholders.py                # npm placeholder check (adds to numbers.json)
python scripts/task2_analysis.py                  # Task 2, stage B -> reports/task2_report.md
python scripts/v4_tables.py                       # tables and figures for v4 -> reports/v4/
```

Environment used: Python 3.11.9, numpy 2.4.4, scipy 1.17.1, statsmodels 0.14.6,
matplotlib 3.10.8, rapidfuzz 3.14.5, pandas 3.0.2, httpx 0.28.1. The analysis scripts block
network access themselves (except the registry check), open databases read-only, refuse to
run on a dirty working tree, and fail if any name appears in a public output.

Tests: `PYTHONPATH=src python -m pytest tests`. Without the private data, tests that read
prompt files or master lists fail with missing files (`test_prompts.py`, three tests in
`test_validators.py`, `test_config.py::test_prompt_files_exist`, and
`test_extractors.py::test_js_require`, which needs the Node core-module list);
`test_config.py::test_budget_sane` fails because of an outdated budget constant.

## Results in one place

- Task 1 (revalidation of the paper numbers): `reports/task1_report.md`, `reports/diff_v3_vs_new.md`
- Task 2 (registrations after exposure): `reports/task2_report.md`
- All numbers, machine-readable: `reports/numbers.json`
- Paper v4 tables and figures: `reports/v4/`

## Responsible disclosure

Name lists are reported privately to the registries' security teams, as described in
`PREREG.md` §5 and `PREREG-deviations.md` (D0).
