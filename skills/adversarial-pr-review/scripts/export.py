"""Export dir for an external (non-Claude) lane: the snapshot's files minus anything denylisted.

Files are extracted from `git archive` with a member filter, so a denylisted file (secrets, keys,
.env) never touches the export dir, and symlinks are dropped rather than followed.
"""

from __future__ import annotations

import io
import subprocess
import tarfile
from collections.abc import Sequence
from pathlib import Path

from common import ReviewError, glob_match


def allowed(path: str, allow_globs: Sequence[str], deny_globs: Sequence[str]) -> bool:
    return glob_match(path, allow_globs) and not glob_match(path, deny_globs)


def export(repo: Path, commit: str, dest: Path, allow_globs: Sequence[str], deny_globs: Sequence[str]) -> list[str]:
    """Write the allowed files of ``commit`` under ``dest``; return the paths left out."""
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True, check=False)
    if data.returncode != 0:
        raise ReviewError(f"git archive {commit} failed: {data.stderr.decode(errors='replace')[-300:]}")
    dest.mkdir(parents=True, exist_ok=True)
    skipped: list[str] = []
    with tarfile.open(fileobj=io.BytesIO(data.stdout)) as tar:
        members = []
        for member in tar.getmembers():
            if member.isdir():
                continue
            if not member.isfile() or not allowed(member.name, allow_globs, deny_globs):
                skipped.append(member.name)
                continue
            members.append(member)
        tar.extractall(dest, members=members, filter="data")
    return sorted(skipped)
