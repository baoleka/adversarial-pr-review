"""Parse `git diff` output into per-file added and removed line sets and hunks."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from common import git

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
# git names the missing side of an added or deleted file with the POSIX null device.
NULL_SIDE = os.devnull


@dataclass
class FileDiff:
    path: str
    old_path: str | None
    status: str  # added | deleted | modified | binary
    added: dict[int, str] = field(default_factory=dict)  # head line -> text
    removed: dict[int, str] = field(default_factory=dict)  # base line -> text
    hunks: list[tuple[int, int, int, int]] = field(default_factory=list)  # (old_start, old_len, new_start, new_len)

    @property
    def changed_lines(self) -> int:
        return len(self.added) + len(self.removed)

    def base_line_changed(self, line: int) -> bool:
        return line in self.removed

    def nearest_hunk_line(self, line: int) -> int:
        """Head-side line of the hunk closest to ``line`` (used to anchor omissions)."""
        if not self.hunks:
            return max(1, line)
        best = min(self.hunks, key=lambda h: min(abs(line - h[2]), abs(line - (h[2] + max(h[3], 1) - 1))))
        start = best[2]
        end = best[2] + max(best[3], 1) - 1
        return min(max(line, start), end) if best[3] else max(1, start)


def parse(diff_text: str) -> dict[str, FileDiff]:
    files: dict[str, FileDiff] = {}
    current: FileDiff | None = None
    old_line = new_line = 0
    old_name: str | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("diff --git "):
            current = None
            old_name = None
            continue
        if raw.startswith("--- "):
            old_name = None if raw[4:] == NULL_SIDE else raw[6:] if raw.startswith("--- a/") else raw[4:]
            continue
        if raw.startswith("+++ "):
            new_name = None if raw[4:] == NULL_SIDE else raw[6:] if raw.startswith("+++ b/") else raw[4:]
            path = new_name or old_name or ""
            status = "added" if old_name is None else "deleted" if new_name is None else "modified"
            current = files.setdefault(path, FileDiff(path=path, old_path=old_name, status=status))
            continue
        if raw.startswith("Binary files "):
            match = re.match(r"Binary files (?:a/)?(.*?) and (?:b/)?(.*?) differ", raw)
            if match:
                path = match.group(2) if match.group(2) != NULL_SIDE else match.group(1)
                files[path] = FileDiff(path=path, old_path=match.group(1), status="binary")
            continue
        hunk = HUNK.match(raw)
        if hunk and current is not None:
            old_start, old_len = int(hunk.group(1)), int(hunk.group(2) or "1")
            new_start, new_len = int(hunk.group(3)), int(hunk.group(4) or "1")
            current.hunks.append((old_start, old_len, new_start, new_len))
            old_line, new_line = old_start, new_start
            continue
        if current is None or not raw:
            continue
        tag, text = raw[0], raw[1:]
        if tag == "+":
            current.added[new_line] = text
            new_line += 1
        elif tag == "-":
            current.removed[old_line] = text
            old_line += 1
        elif tag == " ":
            old_line += 1
            new_line += 1
    return files


def diff_between(repo: Path, base: str, head: str, context: int = 0) -> dict[str, FileDiff]:
    text = git(repo, "diff", "--no-renames", "--no-color", "--no-ext-diff", f"-U{context}", base, head)
    return parse(text)


def function_context_diff(repo: Path, base: str, head: str, paths: list[str] | None = None) -> str:
    """Diff with whole enclosing functions as context (`git diff -W`), for briefs."""
    args = ["diff", "--no-renames", "--no-color", "--no-ext-diff", "-W", base, head]
    if paths:
        args += ["--", *paths]
    return git(repo, *args)
