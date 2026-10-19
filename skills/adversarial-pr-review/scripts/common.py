"""Shared helpers for the review driver: subprocesses, JSON files, hashing and git."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

# The skill's version: the plugin manifest takes it from here, and each run records it.
VERSION = "2.0.4"

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS_DIR = SKILL_DIR / "scripts"
SCHEMAS_DIR = SKILL_DIR / "schemas"
PROMPTS_DIR = SKILL_DIR / "prompts"
AGENTS_DIR = SKILL_DIR / "agents"
REFERENCES_DIR = SKILL_DIR / "references"

# Fixed identity for snapshot commits: the same tree always yields the same commit.
SNAPSHOT_IDENTITY = {
    "GIT_AUTHOR_NAME": "adversarial-review",
    "GIT_AUTHOR_EMAIL": "adversarial-review@localhost",
    "GIT_AUTHOR_DATE": "1970-01-01T00:00:00+0000",
    "GIT_COMMITTER_NAME": "adversarial-review",
    "GIT_COMMITTER_EMAIL": "adversarial-review@localhost",
    "GIT_COMMITTER_DATE": "1970-01-01T00:00:00+0000",
}


class ReviewError(RuntimeError):
    """An error the driver reports and turns into a fail-closed outcome."""


@dataclass(frozen=True)
class Completed:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


def run(
    argv: Sequence[str],
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    check: bool = True,
    input_text: str = "",
    timeout: float | None = None,
    replace_env: bool = False,
) -> Completed:
    """Run a command, capturing stdout and stderr (never discarding either)."""
    full_env = dict(env or {}) if replace_env else {**os.environ, **(env or {})}
    try:
        proc = subprocess.run(
            list(argv),
            cwd=str(cwd) if cwd is not None else None,
            env=full_env,
            input=input_text,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as e:
        raise ReviewError(f"command not found: {argv[0]}") from e
    result = Completed(tuple(argv), proc.returncode, proc.stdout, proc.stderr)
    if check and proc.returncode != 0:
        raise ReviewError(f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.strip()[-2000:]}")
    return result


def run_bytes(argv: Sequence[str], cwd: Path | str | None = None, env: Mapping[str, str] | None = None) -> bytes:
    proc = subprocess.run(
        list(argv),
        cwd=str(cwd) if cwd is not None else None,
        env={**os.environ, **(env or {})},
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise ReviewError(
            f"{' '.join(argv)} exited {proc.returncode}: {proc.stderr.decode('utf-8', 'replace')[-2000:]}"
        )
    return proc.stdout


def git(repo: Path, *args: str, env: Mapping[str, str] | None = None, check: bool = True) -> str:
    return run(["git", "-C", str(repo), *args], env=env, check=check).stdout


def git_ok(repo: Path, *args: str) -> bool:
    return run(["git", "-C", str(repo), *args], check=False).returncode == 0


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def sha256_file(path: Path) -> str:
    return sha256_bytes(path.read_bytes())


def canonical_json(data: Any) -> str:
    """Deterministic JSON: sorted keys, two-space indent, trailing newline."""
    return json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def write_json(path: Path, data: Any) -> str:
    """Write canonical JSON atomically and return its sha256."""
    text = canonical_json(data)
    atomic_write_text(path, text)
    return sha256_text(text)


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def append_jsonl(path: Path, record: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True, ensure_ascii=False) + "\n")


_GLOB_CACHE: dict[str, re.Pattern[str]] = {}


def glob_regex(pattern: str) -> re.Pattern[str]:
    """Compile a path glob: `**/` matches any number of directories, `*` and `?` stay within one
    path segment. A pattern without a slash matches the file name at any depth."""
    cached = _GLOB_CACHE.get(pattern)
    if cached is not None:
        return cached
    out = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    body = "".join(out)
    regex = re.compile(f"(?:.*/)?{body}" if "/" not in pattern else body)
    _GLOB_CACHE[pattern] = regex
    return regex


def glob_match(path: str, globs: Sequence[str] | Iterable[str]) -> bool:
    return any(glob_regex(g).fullmatch(path) for g in globs)


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
