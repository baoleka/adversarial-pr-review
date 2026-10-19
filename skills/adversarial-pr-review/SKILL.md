---
name: adversarial-pr-review
description: Adversarial review of the current branch or working-tree change before it ships. Gates and mechanical checks run first; independent read-only review lanes look for defects and prove them with executed repros; a fresh verifier tries to refute each finding; a deterministic driver computes the verdict (APPROVE, REQUEST_CHANGES or ESCALATE) and, unless asked for a report only, runs a bounded fix loop. Use when asked to review, grill or gate a change or PR, or to "be strict but don't flag pre-existing problems".
compatibility: Claude Code only (terminal or cloud sessions) with Bash, in a git repository. Needs Python 3.10+ and git; Docker is optional (it runs the repros).
allowed-tools: Bash(python3 -E -s ${CLAUDE_SKILL_DIR}/scripts/review.py *), Read, Grep, Glob, Edit, Write
hooks:
  PreToolUse:
    - matcher: "*"
      hooks:
        - type: command
          command: python3
          args: ["-E", "-s", "${CLAUDE_PLUGIN_ROOT}/skills/adversarial-pr-review/scripts/hooks/guard.py"]
          timeout: 10
---

# Adversarial review (v2)

A deterministic driver, `review.py`, owns the whole review: snapshot, gates, review lanes, replay,
verification, verdict and the fix loop. You don't review the code yourself and you don't decide
what is real. You run the actions the driver prints, fix code when it asks, and report its verdict.

This needs Claude Code with the Bash tool, inside a git repository. In claude.ai chat, in Cowork,
or without Bash, say that the review needs Claude Code in a git repository, and stop.

## Start

Run one Bash command:

```
python3 -E -s ${CLAUDE_SKILL_DIR}/scripts/review.py start [--no-fix] '<intent>'
```

- `'<intent>'`: one or two sentences on what the change is meant to do, in the user's words when
  they gave them (for a refactor, say that behavior must not change). With no intent, pass
  nothing; the driver drafts one from the commit messages.
  - Keep it in single quotes, so characters like `(`, `;` and `$` reach the driver as text.
  - If it contains a single quote, reword that part ("does not" for "doesn't").
  - Put every option before it: the driver refuses an option that comes after the intent.
- `--no-fix`: report only. Use it when the user asks for a review without changes.
- `--base-ref <ref>`: the branch to compare against, when the user names one.
- `--budget <usd>`: only if the user sets a spending limit.

The change under review is everything on the current branch since its merge-base with the base
branch, plus uncommitted and untracked work.

If this first command is denied because the skill's folder is outside the session's working
directories, don't retry it (any driver command would be denied the same way). Tell the user to add
the skill's folder (for a plugin, its folder under `~/.claude/plugins/cache/`) to
`permissions.additionalDirectories` in `~/.claude/settings.json` and restart Claude Code.

## Setup check

When the user asks to check the setup, or a review stops on an environment problem, run:

```
python3 -E -s ${CLAUDE_SKILL_DIR}/scripts/review.py doctor
```

with the Bash tool's timeout set to 300000 ms. Report each check whose `ok` is `false`, with its
`fix`. It makes one small probe call (a few cents); add `--offline` to skip it, or `--models` to
also check access to every configured model.

## Loop

Every driver command prints one JSON object, `{"run": ..., "action": {...}}`. Do what the action's
`kind` says, then continue with the next action it prints:

- `exec`: run `action.command` exactly, as one Bash command.
- `wait`: run `action.command` exactly, with the Bash tool's timeout set to `action.timeout_ms`. It
  blocks while background work (gates, review lanes, replays) runs, for up to nine minutes, and
  may print another `wait`. Waiting is normal; don't interrupt it or do other work meanwhile.
- `author` (loop mode only): the action lists findings to address, each with its location,
  failure scenario, repro and suggested fix. For each one, either fix it in the working tree,
  following the codebase's conventions (say why when you diverge from the suggested fix), or
  dispute it with evidence (a `file:line` quote or an executed result). Record each decision with
  the `dispose` command the action shows, then run the action's continue command.
- `ask`: ask the user `action.question`, then run `action.ingest` with `THE ANSWER` replaced by
  their answer. In an unattended run (no user to answer, such as a scheduled task), treat it as an
  escalation: stop and report the question.
- `done` or `escalate`: stop the loop and run `action.command` once to get the summary.

## Rules

- Never write, edit or delete anything under the run dir (`.reviews/`). Only the driver writes
  there, and it detects any other change and escalates.
- Never review the change yourself, spawn reviewer agents, or run lane or replay commands by
  hand. The driver runs the lanes, with their own isolation.
- Don't re-judge findings. You can't mark a finding refuted, and you can't dispute a gate or
  check finding (a failing linter, typechecker, test, changelog or suppression check); fix those.
- Don't push, open or comment on pull requests, or change remote state.

## Report

Report from the summary only:

- the verdict and its reason;
- for REQUEST_CHANGES: each blocking and must-address finding with `file:line` and one line on the
  failure;
- for ESCALATE: each escalation condition and the decision it needs from the user;
- what you fixed or disputed (loop mode), and where you diverged from a suggested fix;
- the path of `report.md`.
