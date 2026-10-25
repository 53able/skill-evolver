<img src="assets/icon.svg" width="64" height="64" alt="">

# skill-evolver

Language: English | [日本語](docs/ja-JP/README.md)

Compare GA, sequential improvement and the original skill in isolated Docker trials with fixed budgets. The CLI uses Python 3.11+ and the standard library. Start with the bundled mock fixture to verify the workflow; use real models and held-out cases to measure skill improvement.

## Quick start

Run Python through uv. From the repository, sync the locked environment and use `PYTHONPATH=src uv run --locked python -m skill_evolver`:

```sh
uv sync --locked
PYTHONPATH=src uv run --locked python -m skill_evolver validate experiments/pilot/config.json
PYTHONPATH=src uv run --locked python -m skill_evolver smoke experiments/pilot/config.json --output /tmp/skill-evolver-smoke
PYTHONPATH=src uv run --locked python -m skill_evolver run experiments/pilot/config.json --output /tmp/skill-evolver-run
PYTHONPATH=src uv run --locked python -m skill_evolver resume /tmp/skill-evolver-run
PYTHONPATH=src uv run --locked python -m skill_evolver report /tmp/skill-evolver-run
PYTHONPATH=src uv run --locked python -m unittest discover -s tests -v
SKILL_EVOLVER_DOCKER_TESTS=1 PYTHONPATH=src uv run --locked python -m unittest discover -s tests -v
```

The uv project sets `package = false`, so checkout commands import `src` directly without an editable installation. There are no runtime dependencies. To install a wheel and use its console command in a separate environment:

```sh
uv venv /tmp/skill-evolver-cli
uv pip install --python /tmp/skill-evolver-cli/bin/python --no-build-isolation .
uv run --no-project --python /tmp/skill-evolver-cli/bin/python skill-evolver validate experiments/pilot/config.json
```

## Execution rules

Start Docker and make `alpine:3` available locally before running trials. The sandbox pins its immutable image ID for the entire experiment. Smoke executes the first three search cases. Use a separate output directory from a full run. `resume` checks the frozen configuration/source and reuses recorded results. Exit 2 means incomplete evaluation; exit 1 means invalid configuration or an operational error. Reports are `report.json` (all cases, budgets, diffs) and `report.md`.

Experiment, case and group IDs must match `[A-Za-z0-9][A-Za-z0-9_-]{0,127}`. Each experiment freezes its immutable Docker image ID before its first trial and its observed environment hash before accepting comparable scores. Resume verifies that exact image and recovers only durably recorded owned resources before new trials.

Start a new run if an older run directory has no frozen image ID; it cannot resume. Reports mark missing runtime freezes, mixed environments and unresolved search execution incomplete, even if final trials succeeded. A successful infrastructure retry resolves its own prior failure; an unknown proposal or exhausted infrastructure retry keeps the comparison incomplete even when the original finishes its final trials.

See [the protocol](docs/experiments/protocol.md) for split grouping, budget/resume behavior, frozen selection and conditional group bootstrap limitations. The fixture has three cases per split and verifies workflow execution. Build separate search, selection and final datasets for a quality experiment.

## OpenAI experiments

To run an OpenAI experiment, configure both `model` and `proposal_model`:

```json
{"provider":"openai","id":"YOUR_SUPPORTED_MODEL_ID","options":{},"api_key_env":"OPENAI_API_KEY","count_fee_acknowledged":true}
```

Set `pricing.version` to your recorded pricing version and `pricing.per_token` to a positive conservative USD/token rate covering both input and output. Freeze search/selection/final token and monetary budgets before running. `validate` checks the contract without requiring a key or contacting a provider; actual calls require the named host environment variable. The broker uses the fixed OpenAI Responses endpoint, exact input-token preflight, bounded output and host-held credentials; arbitrary HTTP endpoints/options are rejected. Proposals preserve metadata and receive only search failure observations. For GA, the code crosses matching sections before requesting a mutation. Responses and usage are journaled before candidates enter search.

The ledger limits spending calculated from your configured token rate. Check provider pricing and token-count request fees separately before setting budgets; `count_fee_acknowledged` records this requirement. Live API compatibility and real-model improvement are unverified. Reports assess improvement only when both roles use real models; any mock role restricts the report to workflow results.

## Documentation

- [Experiment protocol](docs/experiments/protocol.md)
- [OpenAI Responses adapter](docs/model-evidence.md)
- [Docker runtime](docs/runtime-evidence.md)
- [Experiment verification](docs/experiments/report-001.md)

## License

[MIT](LICENSE)
