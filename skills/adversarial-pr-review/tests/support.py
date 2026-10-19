"""Test support: put scripts/ on sys.path, isolate git, and build throwaway fixture repos."""

from __future__ import annotations

import atexit
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
SCRIPTS = SKILL_DIR / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

_EMPTY_GITCONFIG = Path(tempfile.gettempdir()) / "adrev-test-gitconfig"
_EMPTY_GITCONFIG.write_text("", encoding="utf-8")
GIT_ENV = {
    "GIT_CONFIG_GLOBAL": str(_EMPTY_GITCONFIG),
    "GIT_CONFIG_NOSYSTEM": "1",
    "GIT_AUTHOR_NAME": "Test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "Test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_AUTHOR_DATE": "2026-01-01T00:00:00+0000",
    "GIT_COMMITTER_DATE": "2026-01-01T00:00:00+0000",
}
os.environ.update(GIT_ENV)

# Tests never call the real model: a fake `claude` comes first on PATH. Paid tests (opt-in with
# ADREV_PAID_TESTS=1) use REAL_CLAUDE explicitly.
REAL_CLAUDE = shutil.which("claude")
FAKE_CLAUDE = Path(__file__).resolve().parent / "fakes" / "fake_claude.py"
FAKE_BIN = Path(tempfile.mkdtemp(prefix="adrev-fake-bin-"))
atexit.register(shutil.rmtree, FAKE_BIN, True)
(FAKE_BIN / "claude").write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CLAUDE}" "$@"\n', encoding="utf-8")
(FAKE_BIN / "claude").chmod(0o755)
os.environ["PATH"] = f"{FAKE_BIN}{os.pathsep}{os.environ.get('PATH', '')}"


def sh(cwd: Path, *argv: str, check: bool = True) -> str:
    proc = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True, check=False)
    if check and proc.returncode != 0:
        raise AssertionError(f"{argv} failed: {proc.stderr}")
    return proc.stdout


class FixtureRepo:
    """A repo with master, origin/master, origin/HEAD and a feature branch checked out."""

    def __init__(self, root: Path) -> None:
        self.root = root

    @classmethod
    def create(cls, parent: Path, files: dict[str, str], name: str = "repo") -> FixtureRepo:
        root = parent / name
        root.mkdir()
        repo = cls(root)
        repo.git("init", "-q", "-b", "master")
        for rel, text in files.items():
            repo.write(rel, text)
        repo.git("add", "-A")
        repo.git("commit", "-q", "-m", "base")
        repo.git("remote", "add", "origin", "nowhere.invalid:fixture")
        repo.git("update-ref", "refs/remotes/origin/master", "HEAD")
        repo.git("symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/master")
        repo.git("checkout", "-q", "-b", "feature")
        return repo

    def git(self, *args: str, check: bool = True) -> str:
        return sh(self.root, "git", *args, check=check)

    def write(self, rel: str, text: str) -> Path:
        path = self.root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def commit(self, message: str = "change") -> str:
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message)
        return self.git("rev-parse", "HEAD").strip()
