"""Load review.config.json from the merge-base (principle 5) and fill in defaults.

The change under review must not steer its own review, so the config is read from the merge-base
commit. When the merge-base has no config, the head copy is used and a review-integrity finding
is raised by the caller.
"""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common import SCHEMAS_DIR, ReviewError, git, git_ok, read_json, sha256_text
from jsonschema_lite import validate

CONFIG_PATHS = ("review.config.json", ".claude/review.config.json")

DEFAULT_LANE = {"enabled": True, "timeout_s": 1200, "max_turns": 60}
DEFAULTS: dict[str, Any] = {
    "repo": {
        "base_ref": None,
        "remote": "origin",
        "python_paths": [],
        "import_checks": [],
        "gates": [],
        "checks": {
            "ignore_codes": {"enabled": False},
            "tests_run": {"enabled": False},
            "diff_coverage": {"enabled": False},
        },
        "severity_map": {},
        "changelogs": [],
        "conventions_docs": [],
        "file_classes": {},
        "review_inputs": [],
        "tool_tables": ["tool.mypy", "tool.ruff", "tool.pytest", "tool.coverage", "tool.black", "tool.pyright"],
        "sensitive_paths": [],
        "security_patterns": [],
        # Text in the change that addresses reviewers or AI tools (possible prompt injection). Hits
        # trigger the security lane and are pointed out to every lane; they are not findings.
        "injection_patterns": [
            r"(?i)\b(ai|llm|automated|code)[- ]review(er|ers|ing)?\b",
            r"(?i)\bignore (all |any )?(the )?(previous|prior|above|earlier) instructions\b",
            r"(?i)\b(approve|lgtm|ship) (this|the) (change|pr|pull request|diff|patch)\b",
            r"(?i)\b(do not|don't|never) (flag|report|review|mention)\b",
            r"(?i)\b(system prompt|language model|as an ai)\b",
        ],
        "diff_coverage_threshold": 0.8,
    },
    "lanes": {
        "breaker": {"model": "claude-opus-5-5", "effort": "xhigh", **DEFAULT_LANE, "max_turns": 80},
        "external": {"model": "claude-sonnet-5-5", "effort": "high", **DEFAULT_LANE, "max_turns": 80},
        "contracts": {"model": "claude-opus-5-5", "effort": "high", **DEFAULT_LANE, "max_turns": 80},
        "tests": {"model": "claude-sonnet-5-5", "effort": "high", **DEFAULT_LANE},
        "security": {"model": "claude-opus-5-5", "effort": "high", **DEFAULT_LANE, "max_turns": 80},
        "interface": {"model": "claude-opus-5-5", "effort": "high", **DEFAULT_LANE},
        "delta": {"model": "claude-opus-5-5", "effort": "high", **DEFAULT_LANE},
        "verifier": {"model": "claude-opus-5-5", "effort": "xhigh", **DEFAULT_LANE},
        "fixcheck": {"model": "claude-opus-5-5", "effort": "high", **DEFAULT_LANE, "max_turns": 40},
        "adjudicator": {"model": "claude-fable-5-1", "effort": "high", **DEFAULT_LANE},
        "probe": {"model": "claude-haiku-5-5", "effort": "low", "enabled": True, "timeout_s": 240, "max_turns": 8},
    },
    "external": {
        "provider": "none",
        "codex_bin": "codex",
        "model": None,
        "timeout_s": 1200,
        "allow_globs": ["**"],
        "deny_globs": [".env*", "**/.env*", "**/secrets/**", "**/*.pem", "**/*.key"],
    },
    "replay": {
        "isolation": "auto",
        "require_network_isolation": True,
        # Pull the pinned image (by digest, from Docker Hub) when Docker works but the image is missing.
        "auto_pull": True,
        "image": "ubuntu:24.04@sha256:68ee22004a9109b44973313b02ff7b1de250f1de0f5ecb628d73f579aaff33ac",
        "repro_dir": "tests",
        "timeout_s": 120,
        "output_cap_bytes": 65536,
        "allow": [["python", "-m", "pytest", "{repro}"], ["python", "{repro}"]],
    },
    "flaky_reruns": 3,
    "limits": {
        "size_thresholds": {"small_lines": 50, "medium_lines": 200, "max_files": 5},
        "unit_size_cap": 400,
        "max_review_lines": 2000,
        "max_rounds": 3,
        "min_successful_lanes": 2,
        "late_finding_min_severity": "major",
        "full_rereview_delta_ratio": 0.5,
        "verify_cap": 5,
        "estimate_scale": 0.6,
        "max_parallel_calls": 4,
        "budget": {"per_run_usd": 15.0, "per_run_loop_usd": 30.0},
        "untracked_size_cap": 1048576,
        "stale_fetch_age_hours": 72,
        "retention": {"keep_runs": 5, "keep_worktrees": False},
        "run_dir": ".reviews",
        "claude_bin": "claude",
    },
    "pricing": {
        "as_of": "2026-10-06",
        "source": "claude-api skill model table; cache_write = 1.25 x input",
        "models": {
            "claude-fable-5-1": {"input": 10.0, "output": 50.0, "cache_read": 0.25, "cache_write": 12.5},
            "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
            "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
            "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125},
        },
    },
    "overrides": {"known_preexisting": []},
}


@dataclass
class LoadedConfig:
    data: dict[str, Any]
    source: str  # "merge-base", "head" or "defaults"
    path: str | None
    sha256: str | None
    errors: list[str] = field(default_factory=list)

    def get(self, *keys: str) -> Any:
        node: Any = self.data
        for key in keys:
            node = node[key]
        return node


def merge(defaults: Any, override: Any) -> Any:
    """Deep-merge ``override`` onto ``defaults`` (dicts merge, everything else replaces)."""
    if isinstance(defaults, dict) and isinstance(override, dict):
        out = copy.deepcopy(defaults)
        for key, value in override.items():
            out[key] = merge(out[key], value) if key in out else copy.deepcopy(value)
        return out
    return copy.deepcopy(override)


def schema() -> dict[str, Any]:
    data = read_json(SCHEMAS_DIR / "config.schema.json")
    assert isinstance(data, dict)
    return data


def parse(text: str, where: str) -> tuple[dict[str, Any], list[str]]:
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as e:
        return {}, [f"{where}: invalid JSON: {e}"]
    errors = [f"{where}: {err}" for err in validate(raw, schema())]
    if not isinstance(raw, dict):
        return {}, errors or [f"{where}: top level must be an object"]
    return raw, errors


def read_at_rev(repo: Path, rev: str) -> tuple[str, str] | None:
    """Return (path, text) of the config in commit ``rev``, or None."""
    for rel in CONFIG_PATHS:
        if git_ok(repo, "cat-file", "-e", f"{rev}:{rel}"):
            return rel, git(repo, "show", f"{rev}:{rel}")
    return None


def read_in_tree(root: Path) -> tuple[str, str] | None:
    for rel in CONFIG_PATHS:
        path = root / rel
        if path.is_file():
            return rel, path.read_text(encoding="utf-8")
    return None


def load(repo: Path, merge_base: str | None, head_root: Path | None = None) -> LoadedConfig:
    """Load the config from ``merge_base``; fall back to the head copy, then to defaults."""
    found = read_at_rev(repo, merge_base) if merge_base else None
    source = "merge-base"
    if found is None and head_root is not None:
        found = read_in_tree(head_root)
        source = "head"
    if found is None:
        return LoadedConfig(copy.deepcopy(DEFAULTS), "defaults", None, None)
    rel, text = found
    raw, errors = parse(text, f"{source}:{rel}")
    if errors:
        raise ReviewError("invalid review config: " + "; ".join(errors[:10]))
    data = merge(DEFAULTS, raw)
    return LoadedConfig(data, source, rel, sha256_text(text))
