---
name: improve-skill
description: Improve an existing SKILL.md that answers in text with skill-evolver, using the user's Claude Code or Codex subscription login. Generates an evaluation for the user to review, runs up to three bounded revisions only after explicit approval, and reports a candidate without overwriting the original. Use when the user asks to improve, evaluate, or tune an agent skill file. Companion files (references/, assets/, scripts/) are used read-only and scripts are never run. Don't use for skills whose purpose requires running scripts or tools, browsers, or external services.
---

# Improve a skill with skill-evolver

You drive the `skill-evolver improve` workflow for the user. The user decides; you run commands and explain results.

## Rules

- Never pass `--approve-evaluation` until the user has read the evaluation and explicitly approved it in this conversation. After any edit to `evaluation.json`, get approval again. The hash must be `approval_hash` from the latest `improve report <dir> --json`.
- Never change the original `SKILL.md` unless the user explicitly asks to adopt the candidate.
- Only the skill body is improved. The frontmatter (`name`, `description`) and whether the skill gets triggered are neither changed nor evaluated; say so if the user wants better triggering.
- Never reuse an output directory for a new attempt. Failed or interrupted runs are not retried; a new attempt starts with `prepare` in a new directory.
- Always pass `--json` and read fields from the JSON on stdout. Progress lines go to stderr.
- The commands start `claude` or `codex` as child processes, which need network access and write to the CLI's own state directory. In a sandboxed agent (for example Codex's default sandbox), ask the user for permission to run `skill-evolver` outside the sandbox before step 3.

## Steps

1. Check the command: `command -v skill-evolver`. If missing, tell the user to run `uv tool install <path to the skill-evolver repository>` (then `uv tool update-shell` and a new shell if the command is still not found) and stop.
2. Collect inputs:
   - Target: absolute path to the `SKILL.md` to improve.
   - Provider: `claude` when you are Claude Code, `codex` when you are Codex, unless the user chooses otherwise.
   - Model: ask the user. The same model generates the evaluation, runs the tasks, proposes revisions and judges results. Claude accepts aliases such as `sonnet`; Codex needs the full model ID shown by `/model`.
   - Output: a new directory outside the skill folder, `~/skill-evolver-runs/<skill-name>-<YYYYMMDD-HHMMSS>`.
3. Prepare (one model call plus CLI checks; allow five minutes):
   `skill-evolver improve prepare <SKILL.md> --provider <provider> --model <model> --output <dir> --json`
   Claude Code: set the Bash tool `timeout: 300000`; the default two minutes can cut it off. Other agents: use a timeout of at least five minutes.
   - On failure, show the error and the hint line printed under it on stderr, then stop. For `unsupported_cli_version`, ask the user before trying again with `--allow-untested-cli` and a new output directory.
   - If `state` is `unsupported`, explain `reason` and stop.
   - If `warnings` says the evaluation is too weak, explain that `run` will refuse it until `evaluation.json` has 3-12 criteria with at least one required, and offer to edit it with the user.
4. Review with the user. Give them the path `<dir>/evaluation.md` so they can read it in full, and summarize: purpose, fixed constraints, criteria (mark required ones), and the three development and three held-out prompts. Ask them to check:
   - Do the prompts look like real uses of the skill?
   - Are the required criteria the ones that matter, neither too lax nor impossible?
   - Would the current skill already pass everything? Then no improvement can be shown. Ask the user for a request where the skill actually fell short, and use it to replace one case, or add the observed failure as a required criterion.
   - Are the held-out prompts different in substance from the development prompts?
   Ask whether to approve, edit, or stop. To edit, change `<dir>/evaluation.json` only: keep exactly three cases per split (replace, do not add), unique `id`/`group_id` values, and 3-12 criteria with at least one `required: true`. Then run `skill-evolver improve report <dir> --json`, summarize the changes, and ask for approval again.
5. Run only after explicit approval:
   `skill-evolver improve run <dir> --approve-evaluation <approval_hash> --json > <dir>.out 2> <dir>.log`
   It makes about 45 model calls (up to 60 with timeout retries) and can take up to one hour. Do not let a tool timeout kill it:
   - Claude Code: run it with the Bash tool's `run_in_background: true` and `timeout: 3900000`.
   - Other agents: start it detached with `nohup ... &` and the same redirections.
   Tell the user it is running. Check progress with `tail -n 3 <dir>.log` (lines like `model call 12 (limit 60)`). It has finished when the process has exited (`ps -p <pid>` with the PID from starting it); then `<dir>.out` holds the JSON result, or `<dir>.log` shows the error. Do not run `improve report` while it runs: an `active writer` error only means the run is still going. If your agent is not notified when background work ends, tell the user to ask you to check the result later.
6. When it finishes, run `skill-evolver improve report <dir> --json` and use the top-level `status`:
   - `observed_improvement`: show `diff -u <source_path> <candidate_path>` and summarize what changed. Ask whether to adopt it. If yes, first check that `warnings` does not say the original changed, then back up with `cp <source_path> <source_path>.bak-$(date +%Y%m%d-%H%M%S)` and run `cp <candidate_path> <source_path>`.
   - `no_observed_improvement`: say no improvement was observed and the original was not changed. Run `skill-evolver improve report <dir>` (without `--json`) and show its score lines; they explain when the evaluation was too easy or a revision newly failed a required check.
   - `incomplete`: show `reason` and `warnings`; suggest a new `prepare` in a new directory.
   Say that results come from three development and three held-out tasks written and judged by a model: they are observations, not proof of general improvement.
