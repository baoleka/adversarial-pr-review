"""The interface pass for large changes: changed public signatures and their callers outside the
diff, so a defect at a unit boundary isn't missed by lanes that each see one unit."""

from __future__ import annotations

import re
from collections.abc import Sequence
from pathlib import Path

from triage import file_at, public_api

from common import run

MAX_CALLERS_PER_NAME = 20
MAX_CALLERS = 200
_CHANGE = re.compile(r"^(?P<path>.+?):(?P<name>[\w.]+) \((?P<status>added|removed|changed)\)$")


def callers(repo: Path, commit: str, name: str, exclude: Sequence[str]) -> list[str]:
    """`file:line: text` for each line mentioning ``name`` as a word at ``commit``, outside ``exclude``."""
    word = name.rsplit(".", 1)[-1]
    if word == "__init__":
        word = name.split(".")[0]
    out = run(["git", "-C", str(repo), "grep", "-n", "-w", "-I", "-e", word, commit, "--", "*.py"], check=False)
    found = []
    for line in out.stdout.splitlines():
        _commit, _, rest = line.partition(":")
        path, _, rest = rest.partition(":")
        number, _, text = rest.partition(":")
        if path in exclude:
            continue
        found.append(f"{path}:{number}: {text.strip()[:160]}")
        if len(found) >= MAX_CALLERS_PER_NAME:
            break
    return found


def summary(
    repo: Path, base: str, head: str, changes: Sequence[str], changed_files: Sequence[str]
) -> tuple[str, list[str]]:
    """Markdown describing each changed public name and its outside callers, and the caller files."""
    blocks: list[str] = []
    caller_files: set[str] = set()
    total = 0
    apis: dict[tuple[str, str], dict[str, str]] = {}
    for change in changes:
        match = _CHANGE.match(change)
        if not match:
            continue
        path, name, status = match["path"], match["name"], match["status"]
        for side, rev in (("base", base), ("head", head)):
            if (path, side) not in apis:
                apis[(path, side)] = public_api(file_at(repo, rev, path))
        old, new = apis[(path, "base")].get(name), apis[(path, "head")].get(name)
        lines = [
            f"### `{path}` `{name}` ({status})",
            "",
            f"- base: `{old}`" if old else "- base: (absent)",
            f"- head: `{new}`" if new else "- head: (removed)",
        ]
        found = callers(repo, head, name, changed_files) if total < MAX_CALLERS else []
        total += len(found)
        caller_files |= {f.split(":", 1)[0] for f in found}
        lines.append("- Callers outside the diff:" if found else "- Callers outside the diff: none found")
        lines += [f"  - `{f}`" for f in found]
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks), sorted(caller_files)
