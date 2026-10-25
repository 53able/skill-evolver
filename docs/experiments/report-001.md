# Mock experiment verification — 2026-10-25

Language: English | [日本語](../ja-JP/experiments/report-001.md)

The deterministic mock model executes a public `uppercase` operation with real Docker tools. The results establish workflow execution and resume behavior. Measuring skill quality or GA effectiveness requires a separate real-model experiment. This verification made no live paid model calls.

## Workflow results

A reduced experiment used population 3, one generation, one independent search, three search cases, one selection case and one final case. Selection and final evaluation each used three repeats.

`validate`, a separate-directory three-case `smoke`, `run`, `resume` and `report` completed successfully through uv. Original, sequential and GA completed their final trials. The journal contained 39 trial records before and after resume, with 39 resource allocations and 39 releases. No completed trial was repeated.

The experiment froze these runtime identifiers:

- Image: `sha256:f7bbaec1bd0330a8dba26f03e05f421bb3c8b807bb41873df3c3b0c3d1083d8f`
- Environment: `f65d337e375d6c9e8d37fd1405eb11d7be09eb4410c3f39b6156c0898d4a8140`
- Configuration: `0a84db2e420cd8a445a0ec40de47d790950a990d8eaa8edb15abd067bbfb7fb1`

The run directory was `/tmp/skill-evolver-core-repair-e2e`. It contains `experiment.json`, `journal.jsonl`, candidate differences, `report.json` and `report.md`. Temporary run directories are local test outputs rather than portable datasets.

## Verification

```sh
SKILL_EVOLVER_DOCKER_TESTS=1 PYTHONPATH=src uv run --locked python -m unittest discover -s tests -q
uv tool run --from ruff==0.14.14 ruff check --select F,E9 src tests _build_backend.py
uv tool run --from mypy==1.19.1 mypy --check-untyped-defs --ignore-missing-imports src/skill_evolver
```

All 73 tests passed, including Docker integration, with no skips. Ruff passed; mypy passed for all 14 source modules. Tests cover identifier collisions, runtime freezing, storage and process limits, exact resource recovery, phase separation, interrupted calls, known and unknown usage, and conditional comparison reporting. Unknown usage remains incomplete even when stop or cleanup also fails and a later attempt succeeds.

`uv sync --locked`, CLI validation, ordinary wheel installation through `uv pip`, and the installed console command also succeeded.

## Limits

Live OpenAI compatibility and real-model skill improvement remain unverified. HTTP and proposal tests use mocked transport or injected brokers. Before a paid experiment, set token rates from provider pricing and check token-count request fees separately. The ledger accounts for the configured rates.

Group related evaluation cases before splitting the dataset. Improvement judgments require at least ten independent final groups. Local bundled skill assets remain unsupported. Historical runs without a frozen image ID cannot resume as valid current experiments.
