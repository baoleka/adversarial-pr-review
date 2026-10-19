# Forbidden strings and literals

The guard test (`tests/test_forbidden.py`) fails if any of these appear in the skill tree or in the
evals code and configs. Item data (`evals/patches/`, `evals/hidden_tests/`) is skipped, because git
patches legitimately name the null device on the missing side of an added file.

Permission bypasses and unsafe modes (never used, never suggested):

- `dangerously` (any `--dangerously-*` flag)
- `bypassPermissions`
- `danger-full-access`
- `yolo`
- `approve-for-me`
- `sandbox_mode`
- `--trust-plugin`

Discarded output (stdout and stderr always go to files):

- the POSIX null device path
- Python's `DEVNULL`

Credentials in code: an assignment to `CODEX_API_KEY`, `ANTHROPIC_API_KEY` or `OPENAI_API_KEY`.

Repo literals that belong in `review.config.json`, not in the skill: the stand-in's conventions
file name, its second changelog's name, the stand-in package name, v1's agent namespace and v1's
named pre-existing problems. The generic base-branch fallback list in `snapshot.py` is allowed.
