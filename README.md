<img src="assets/icon.svg" width="64" height="64" alt="">

# skill-evolver

Language: English | [日本語](docs/ja-JP/README.md)

Improve a self-contained text skill using your existing Claude Code or Codex login. The tool creates an evaluation for you to review, tries up to three purpose-preserving revisions, and saves a candidate plus a comparison report. It uses Python 3.11+ and the standard library. This workflow does not require Docker or a separate API key.

## Getting started

Setup takes about five minutes. An improvement run takes from a few minutes up to one hour, depending on the model.

### 1. Install and log in

Install [uv](https://docs.astral.sh/uv/getting-started/installation/), then install this repository.

```sh
uv tool install /path/to/skill-evolver   # path to this repository; add --reinstall after updating it
skill-evolver --help                     # if not found, run uv tool update-shell and open a new shell
```

Log in to `claude` or `codex` with your subscription as usual. No API key is needed.

### 2. Easiest: ask your agent

Copy the bundled skill to use the tool from a Claude Code or Codex conversation.

```sh
mkdir -p ~/.claude/skills && cp -R /path/to/skill-evolver/skills/improve-skill ~/.claude/skills/   # Claude Code
mkdir -p ~/.codex/skills && cp -R /path/to/skill-evolver/skills/improve-skill ~/.codex/skills/     # Codex
```

In a new conversation, ask: "Improve `~/.claude/skills/my-skill/SKILL.md` with improve-skill." (In Codex you can also type `$improve-skill`.) The agent prepares an evaluation, summarizes it, runs the improvement **only after you approve**, and shows you the resulting diff. Your original skill is not changed unless you say you want to adopt the candidate.

In Codex's default sandbox, the `codex`/`claude` processes this tool starts cannot reach the network. When the agent asks to run outside the sandbox, review the request and allow it.

### 3. Run it yourself (two commands)

```sh
# Create the evaluation (one model call, usually under two minutes)
skill-evolver improve prepare ~/.claude/skills/my-skill/SKILL.md \
  --provider claude --model sonnet --output ~/skill-evolver-runs/my-skill-$(date +%Y%m%d-%H%M%S)

# Show the evaluation; answer y to run (use the directory printed above)
skill-evolver improve run ~/skill-evolver-runs/my-skill-20261025-120000
```

Keep the terminal open while `run` works; an interrupted run cannot be resumed. Each command prints the current state and the next command to type. When the run completes, it prints the per-split scores and the comparison report (`comparison.md`). If an improvement was observed, it also prints the improved skill (`candidate/SKILL.md`) and the `diff`/`cp` commands to adopt it. For Codex, pass `--provider codex --model <full model ID shown by /model in codex>`. The chosen model writes the evaluation, runs the tasks, proposes revisions and judges the results.

### What to check in the evaluation

Read `evaluation.md` before approving. The evaluation decides what counts as better.

- Do the tasks look like real uses of the skill?
- Are the required criteria the ones that matter, neither too lax nor impossible?
- Would the current skill already pass everything? Then no improvement can be shown. Make it harder by replacing one task with a request where the skill actually fell short, or by adding an observed failure as a required criterion.
- Are the held-out tasks different in substance from the development tasks?

To change it, edit `evaluation.json` and run `skill-evolver improve report <directory>` before `run` (`evaluation.md` is a generated preview; editing it has no effect). Keep exactly three development and three held-out tasks (replace, do not add), unique `id`/`group_id` values, and 3-12 criteria with at least one marked required (`run` refuses weaker evaluations).

## Details

**How it works:** From your skill, the tool drafts the purpose, constraints to keep, pass/fail criteria, and two task sets (development and held-out, three each). It tries up to three revisions on the development tasks, then compares the best candidate with the original on the held-out tasks.

**Reading results:** `observed_improvement` (improvement observed on the reviewed evaluation), `no_observed_improvement`, or `incomplete`. Improvement is reported only when the held-out score rises with no new failures on required criteria. With three tasks per split, written and judged by a model, a single extra pass counts; this is not proof of general or statistical superiority.

**CLI versions:** Claude Code 2.1.310 and Codex 0.164.0 are tested; other versions stop. To try one anyway, add `--allow-untested-cli` to `prepare`. Required-option and subscription-login checks still apply, and the report marks the version as untested. If the CLI updates between `prepare` and the end of `run`, that run ends incomplete, so avoid updating it meanwhile. Claude aliases such as `sonnet` are pinned to the model the CLI reports; if it reports none, the run stops, so pass a full model ID.

**What approval freezes:** answering `y` to `run` freezes the purpose, evaluation, model selector, CLI version and invocation settings. A changed input requires a new preparation in a new directory. Re-running a completed directory does not repeat model calls. A task execution or judging call that times out is retried once, since it changes nothing; other failed, interrupted or uncertain calls are not retried. For scripts, add `--json` and pass `approval_hash` from `improve report --json` to `run --approve-evaluation`. Exit codes: 0 success, 1 error, cancel or unsupported skill, 2 incomplete.

**Scope:** skills fully contained in one `SKILL.md` of up to 64 KiB, without companion files. Only the quality of output produced by the body's instructions is improved; the frontmatter (`name`/`description`) and whether the skill gets triggered are neither changed nor evaluated. Code can be judged as text; code execution, browser/service actions and companion files are not supported. A body that contains path-like text with `scripts/`, `references/` or `assets/` (such as `scripts/run.sh` or `app/assets/x`), or relative links to other files, is refused even when the path only appears in prose (URLs and `#heading` links are fine).

**Limits and typical use:** a measured run with `haiku` and a small skill used 43 calls in about five minutes. If the original passes every development check, revisions are skipped to save calls. A directory left by a failed `prepare` cannot be reused and can be deleted. The limits are three revisions, 60 model calls (including the one for prepare), 120 seconds per call, one hour per run, 256 KiB per prompt, 1 MiB each for the CLI's stdout and stderr. CLI-reported usage is recorded when available; unknown usage and costs remain unknown. These limits do not guarantee an exact token, invoice or subscription-quota ceiling. Usage of a call that timed out is not observed, even if the provider kept generating.

**Execution boundary:** the invocation profiles restrict model shell/MCP/web/write actions and reject unexpected tool events. Managed policy hooks, CLI authentication/internal-state updates and complete host-read isolation are outside that boundary. The tool uses your CLI's existing login, removes API-key and endpoint variables from the child environment, and never copies or reads authentication files itself. It does not modify your CLI settings.

**Running without installing:** from the repository root, replace `skill-evolver` with `PYTHONPATH=src uv run --locked python -m skill_evolver`.

## Controlled experiments (research, advanced)

You do not need this section to improve a skill. It describes a separate Docker-based tool for comparing improvement methods.

Compare GA, sequential improvement and the original skill in isolated Docker trials with fixed budgets. The CLI uses Python 3.11+ and the standard library. Start with the bundled mock fixture to verify the workflow; use real models and held-out cases to measure skill improvement.

## Experiment quick start

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
