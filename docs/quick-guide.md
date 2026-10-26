# Quick guide: improve your skill once

Language: English | [日本語](ja-JP/quick-guide.md)

This guide is for people who are unhappy with what one of their skills produces and want to try one improvement first. By the end, you can judge the evaluation yourself and decide whether to adopt the improved skill. See the [README](../README.md) for the full details.

## Supported skills

- **Supported:** a `SKILL.md` of up to 64 KiB whose responses can be judged as text. Companion files such as `references/` and `assets/` are used read-only.
- **Unsupported:** skills whose purpose requires running scripts, browsers or external services. `prepare` stops for these.
- **Not improved:** the frontmatter (`name`/`description`) and whether the skill gets triggered.

## 1. Set up (five minutes)

```sh
uv tool install /path/to/skill-evolver   # if not found, run uv tool update-shell and open a new shell
mkdir -p ~/.claude/skills && cp -R /path/to/skill-evolver/skills/improve-skill ~/.claude/skills/
```

Log in to `claude` (or `codex`) as usual. No API key is needed. For Codex, copy the skill to `~/.codex/skills/` instead.

## 2. Ask your agent

In a new conversation, ask:

> Improve `~/.claude/skills/my-skill/SKILL.md` with improve-skill.

Answer when the agent asks which model to use (for example `sonnet`). The evaluation is ready in about two minutes, and the agent summarizes it.

## 3. Judge the evaluation (the part that matters)

The evaluation alone decides whether the skill improved. Read `evaluation.md` before approving and check four points:

1. Do the tasks look like real uses of the skill?
2. Are the required criteria the ones that matter, neither too lax nor impossible?
3. Would the current skill already pass everything? Then no improvement can be shown. Ask the agent to replace one task with a request where the skill actually fell short.
4. Are the held-out tasks different in substance from the development tasks?

If it looks right, approve it. To change something, say so in the conversation; the agent edits `evaluation.json` and asks for approval again.

## 4. Wait (a few minutes, up to one hour)

After approval, the agent runs the improvement in the background. It makes about 45 model calls, at most 60. When it finishes, ask the agent to check the result.

## 5. Read the result

| Result | Meaning | Next step |
| --- | --- | --- |
| `observed_improvement` | The held-out score rose with no new failures on required criteria | Read the diff and ask to adopt it if you agree (the original is backed up) |
| `no_observed_improvement` | No improvement was observed. The original skill is unchanged | Check the score lines. If the evaluation was too easy, make it harder and prepare again |
| `incomplete` | The evaluation did not finish | Check the reason and prepare again in a new directory |

Each split has only three tasks, written and judged by a model. `observed_improvement` means the skill did better on this evaluation; it is not proof of general improvement. Always read the diff yourself.

## Troubleshooting

- **`skill-evolver` not found:** run `uv tool update-shell` and open a new shell.
- **Stops with `unsupported_cli_version`:** your CLI version is untested. To try anyway, prepare again in a new directory with `--allow-untested-cli`.
- **Codex cannot reach the network:** the default sandbox blocks it. When the agent asks to run outside the sandbox, review the request and allow it.
- **The run stopped or failed:** it cannot be resumed. Start again with `prepare` in a new directory.

## Run it without an agent

```sh
skill-evolver improve prepare ~/.claude/skills/my-skill/SKILL.md \
  --provider claude --model sonnet --output ~/skill-evolver-runs/my-skill-$(date +%Y%m%d-%H%M%S)
skill-evolver improve run ~/skill-evolver-runs/my-skill-20261025-120000   # use the printed directory; answer y to run
```

Keep the terminal open while `run` works. Each command prints the next command to type.
