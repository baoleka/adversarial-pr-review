# adrev: adversarial PR review for Claude Code

`adrev` adds the `/adversarial-pr-review` skill to Claude Code. It reviews the change on your current
branch the way a strict reviewer would, but it proves what it claims. A deterministic driver runs:
- your repo's own checks (lint, typecheck, tests, changelog rules);
- independent, read-only review sessions that try to break the change;
- the repros they write, inside network-less containers;
- a fresh verifier that tries to refute every finding.

It then computes the verdict, APPROVE, REQUEST_CHANGES or ESCALATE, instead of letting a model vote.
Problems that were already there before the change are reported as pre-existing and never block.
Unless you ask for a report only, it also runs a bounded fix loop: it fixes the findings, re-checks
each fix, and stops with APPROVE or ESCALATE.

Version 2.0.1. Licensed under Apache-2.0.

## Install

In Claude Code (terminal):

```
/plugin marketplace add baoleka/adversarial-pr-review
/plugin install adrev@baoleka
```

or from a shell: `claude plugin marketplace add baoleka/adversarial-pr-review`, then
`claude plugin install adrev@baoleka`.

**Everywhere you use Claude Code, cloud sessions included:**
1. On claude.ai, open Customize → Plugins → Add marketplace.
2. Enter `baoleka/adversarial-pr-review` and install `adrev`, with "Sync automatically" on.

Plugins added there sync to the terminal sessions you sign in to with that account, and to your
cloud sessions.

## Use

In a git repository, on the branch you want reviewed:

- `/adversarial-pr-review --no-fix`: report only.
- `/adversarial-pr-review`: review, fix, re-review, until APPROVE or ESCALATE.
- Add one or two sentences of intent after the command, e.g. `/adversarial-pr-review --no-fix
  refactor: split the parser; behavior must not change`. Without one, the intent is drafted from
  your commit messages.
- `--base-ref <ref>` sets the branch to compare against. By default it's `origin/HEAD`, then
  `origin/main`, then `origin/master`.

The change under review is everything on the branch since its merge-base with the base, plus
uncommitted and untracked work. The full report is written to `.reviews/<run>/report.md`, which is
kept out of git automatically.

To check that a machine is ready, ask Claude to "check the adversarial-pr-review setup". It runs
`review.py doctor`, which tests Python, git, Docker, the `claude` CLI and its login, and runs one
tiny confined probe session, then prints a fix for anything that fails.

## Requirements

- **Claude Code 2.1.295 or later**, logged in. The review sessions use the same login.
- **Python 3.10 or later** as `python3` (3.12 recommended), standard library only, and **git 2.31 or
  later**.
- **Linux or macOS.** Repros run only on Linux.
- **Optional: Docker** with a running daemon, to run repros. Without it, findings rest on traced
  code evidence instead of executed tests.
- **Models:** by default Opus 5.5, Sonnet 5.5, Haiku 5.5 (the probe) and Fable 5.1 (rare
  adjudications). Run `doctor --models` to check that your plan can use them; per-repo overrides
  go in `review.config.json`.

## Configure a repository (optional)

Without configuration, adrev runs its review sessions and verification only. To add your CI's
checks, commit a `review.config.json` at the repository root. It is read from the merge-base, so a
change can't weaken its own review. A small example:

```json
{
  "schema_version": "1",
  "repo": {
    "base_ref": "origin/main",
    "remote": "origin",
    "gates": [
      {"name": "ruff", "argv": ["ruff", "check", "--no-fix", "."], "parser": "ruff", "timeout_s": 300},
      {"name": "pytest", "argv": ["pytest", "-q"], "parser": "pytest", "timeout_s": 900}
    ],
    "changelogs": [{"path": "CHANGELOG.md", "globs": ["src/**"], "skip_marker": "changelog: skip"}],
    "conventions_docs": [{"path": "CONTRIBUTING.md"}],
    "file_classes": {"tests": ["tests/**"], "docs": ["docs/**", "*.md"]},
    "sensitive_paths": ["src/auth/**"],
    "security_patterns": ["subprocess", "shell=True", "pickle"]
  }
}
```

The full schema, with gates that use an error-count baseline, coverage, lane models, budgets and
known pre-existing problems, is `skills/adversarial-pr-review/schemas/config.schema.json`.

## Cloud sessions

Each cloud session is a fresh Linux VM, so set the environment up so reviews can run fully:
- **History.** The review needs the merge-base with the base branch. If your clone is shallow, it
  stops and gives the `git fetch` command to run.
- **Repros.** They need a running Docker daemon, plus a Python that isn't the system interpreter
  first on PATH, for example a `uv venv`. Without these, reviews still run, on traced evidence.
- **Unattended runs.** In a scheduled routine, a question from the driver ends the run as
  ESCALATE.

## Data handling

- **Review sessions.** These are separate headless `claude -p` processes started on your machine
  (or in your cloud session) with your Claude Code login. They read the diff and the code at the
  base and head commits. Their requests go to Anthropic, like any Claude Code session.
  - Up to 4 run at once, under a per-run budget cap: $15 for a report and $30 for the fix loop, at
    list prices.
  - They run in restricted mode with an allowlist and a deny-by-default hook. They can write only in
    their own scratch folder.
- **Your repository's checks.** adrev runs the commands listed in your `review.config.json`, on your
  machine, at the base and the head of the change.
- **Containers.** If Docker works but the pinned `ubuntu:24.04` image (pinned by digest) is
  missing, adrev pulls it from Docker Hub. Repros then run with no network, a read-only tree and no
  capabilities.
- **Optional second model family (off by default).** If you enable it, it sends an export of the
  change to OpenAI's Codex, and it refuses to run unless file reads can be confined to that export.
- **Local writes:**
  - `.reviews/` (listed in `.git/info/exclude`);
  - refs under `refs/reviews/`;
  - temporary worktrees, removed after the verdict;
  - in the fix loop, the fixes in your working tree.

  adrev never commits, pushes, comments or changes remote state.
- **Permissions.** The skill pre-approves its own driver command for the turn that invokes it.
  While it's active, a hook stops anything but the driver from writing to `.reviews/`.
- **No telemetry.** Besides the model calls above and the optional image pull, nothing is sent
  anywhere.

## Cost

A report on a small change typically costs $1–2 of model usage, and the fix loop about twice
that. Very large diffs cost more, and are capped by the budget.

## Limitations

- It needs Claude Code with Bash in a git repository; it doesn't run in claude.ai chat or Cowork.
- Repros need Linux and a non-system Python. Paths with spaces aren't supported.
- With `permissions.blockReadsOutsideWorkingDirectories` on, add the plugin's folder
  (`~/.claude/plugins/cache/baoleka/adrev`) to `permissions.additionalDirectories`.
- Gates and checks know Python tooling best (ruff, mypy, pytest, coverage); other tools work
  through the `regex` and `exit` parsers.

## How well it works

adrev v2 was benchmarked against an earlier single-reviewer version of the same skill (v1). The
corpus:
- a stand-in repository (pallets/click 8.1.8);
- planted bugs and re-introduced real upstream bugs;
- clean upstream commits and correct-but-unusual decoys;
- pre-existing problems, a prompt-injection item and large changes.

On a fresh held-out set of 24 items:

| | v1 | v2 |
|---|---|---|
| Bugs found | 100% | 100% |
| False alarms on clean or decoy changes | 86% | 19% |
| Fix loop ends in a correct approval | 79% | 100% |
| Approvals of still-broken code | 0% | 0% |
| Median cost per review | 1× | 1.2× |

On the first held-out set (26 items), v2 had 0% false alarms against 56%, 94% loop success against
69%, and 0% wrong approvals against 19%. The samples are small, so read these as directions rather
than precise rates.

## License

Apache-2.0. See `LICENSE` and `NOTICE`.
