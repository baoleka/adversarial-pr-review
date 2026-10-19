"""Snapshot the working tree, resolve the base, and manage review worktrees.

The snapshot never touches the user's index, HEAD or files: it copies the index to a temporary
file inside the run dir and stages everything into that copy.
"""

from __future__ import annotations

import os
import stat
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from common import SNAPSHOT_IDENTITY, ReviewError, git, git_ok, run

# Generic fallbacks for the base ref, tried after the configured one (§4.1).
DEFAULT_BASE_CANDIDATES = ("{remote}/HEAD", "{remote}/main", "{remote}/master")


@dataclass
class Snapshot:
    tree: str
    commit: str
    ref: str
    skipped_untracked: list[str] = field(default_factory=list)


def repo_root(cwd: Path) -> Path:
    out = run(["git", "-C", str(cwd), "rev-parse", "--show-toplevel"], check=False)
    if out.returncode != 0:
        raise ReviewError(f"{cwd} is not inside a git repository")
    return Path(out.stdout.strip())


def git_path(repo: Path, name: str) -> Path:
    return Path(git(repo, "rev-parse", "--path-format=absolute", "--git-path", name).strip())


def head_commit(repo: Path) -> str | None:
    out = run(["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", "HEAD^{commit}"], check=False)
    return out.stdout.strip() or None


def ensure_excluded(repo: Path, run_dir_rel: str) -> None:
    """Exclude the run dir through info/exclude; never edit .gitignore."""
    exclude = git_path(repo, "info/exclude")
    pattern = "/" + run_dir_rel.strip("/") + "/"
    existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
    if pattern not in existing.splitlines():
        exclude.parent.mkdir(parents=True, exist_ok=True)
        with open(exclude, "a", encoding="utf-8") as fh:
            if existing and not existing.endswith("\n"):
                fh.write("\n")
            fh.write(f"{pattern}\n")


def untracked_files(repo: Path) -> list[str]:
    out = git(repo, "ls-files", "--others", "--exclude-standard", "-z")
    return [p for p in out.split("\0") if p]


def snapshot_tree(repo: Path, scratch: Path, run_dir_rel: str, untracked_cap: int) -> tuple[str, list[str]]:
    """Stage the whole working tree into a temporary index and return (tree sha, skipped files)."""
    scratch.mkdir(parents=True, exist_ok=True)
    skipped = []
    # The run dir is excluded through info/exclude (see ensure_excluded). Naming an ignored path
    # in an exclude pathspec makes `git add` fail, so the check below verifies it instead.
    excludes: list[str] = []
    for rel in untracked_files(repo):
        path = repo / rel
        try:
            size = path.lstat().st_size
        except OSError:
            continue
        if stat.S_ISREG(path.lstat().st_mode) and size > untracked_cap:
            skipped.append(rel)
            excludes.append(f":(exclude,literal){rel}")
    fd, idx = tempfile.mkstemp(dir=scratch, prefix="index.")
    os.close(fd)
    spec_fd, spec = tempfile.mkstemp(dir=scratch, prefix="pathspec.")
    try:
        real_index = git_path(repo, "index")
        if real_index.exists():
            Path(idx).write_bytes(real_index.read_bytes())
        else:
            os.unlink(idx)
        with os.fdopen(spec_fd, "w", encoding="utf-8") as fh:
            fh.write("\0".join([".", *excludes]) + "\0")
        env = {"GIT_INDEX_FILE": idx}
        git(repo, "add", "-A", f"--pathspec-from-file={spec}", "--pathspec-file-nul", env=env)
        tree = git(repo, "write-tree", env=env).strip()
        if git(repo, "ls-tree", "--name-only", tree, "--", run_dir_rel.strip("/")).strip():
            raise ReviewError(f"the run dir {run_dir_rel} would be part of the snapshot; is it excluded?")
    finally:
        for leftover in (idx, spec):
            if os.path.exists(leftover):
                os.unlink(leftover)
    return tree, sorted(skipped)


def commit_tree(repo: Path, tree: str, parent: str | None, message: str = "adversarial-review snapshot") -> str:
    argv = ["commit-tree", "--no-gpg-sign", tree, "-m", message]
    if parent:
        argv += ["-p", parent]
    return git(repo, *argv, env=SNAPSHOT_IDENTITY).strip()


def take_snapshot(
    repo: Path, scratch: Path, run_dir_rel: str, run_id: str, round_no: int, untracked_cap: int
) -> Snapshot:
    tree, skipped = snapshot_tree(repo, scratch, run_dir_rel, untracked_cap)
    commit = commit_tree(repo, tree, head_commit(repo))
    ref = f"refs/reviews/{run_id}/r{round_no}"
    git(repo, "update-ref", ref, commit)
    return Snapshot(tree=tree, commit=commit, ref=ref, skipped_untracked=skipped)


def resolve_base_ref(repo: Path, configured: str | None, remote: str) -> str | None:
    candidates = [configured] if configured else []
    candidates += [c.format(remote=remote) for c in DEFAULT_BASE_CANDIDATES]
    for ref in candidates:
        if ref and git_ok(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"):
            return ref
    return None


def is_shallow(repo: Path) -> bool:
    """True for a shallow clone (history cut at the clone depth), common in CI and cloud sessions."""
    out = run(["git", "-C", str(repo), "rev-parse", "--is-shallow-repository"], check=False)
    return out.stdout.strip() == "true"


def merge_bases(repo: Path, base_ref: str, commit: str) -> list[str]:
    out = run(["git", "-C", str(repo), "merge-base", "--all", base_ref, commit], check=False)
    return [line for line in out.stdout.split() if line]


def stale_base_warning(repo: Path, base_ref: str, remote: str, max_age_hours: int) -> str | None:
    """Warn when the local base branch differs from its tracking ref, or FETCH_HEAD is old."""
    warnings = []
    if base_ref.startswith(f"{remote}/") and not base_ref.endswith("/HEAD"):
        local = base_ref[len(remote) + 1 :]
        local_sha = run(
            ["git", "-C", str(repo), "rev-parse", "--verify", "--quiet", f"refs/heads/{local}"], check=False
        ).stdout.strip()
        remote_sha = git(repo, "rev-parse", f"{base_ref}^{{commit}}").strip()
        if local_sha and local_sha != remote_sha:
            warnings.append(f"local branch {local} ({local_sha[:10]}) differs from {base_ref} ({remote_sha[:10]})")
    fetch_head = git_path(repo, "FETCH_HEAD")
    if fetch_head.exists():
        age_h = (time.time() - fetch_head.stat().st_mtime) / 3600
        if age_h > max_age_hours:
            warnings.append(f"last fetch was {age_h:.0f} hours ago")
    return "; ".join(warnings) or None


def add_worktree(repo: Path, path: Path, sha: str, hooks_dir: Path) -> None:
    hooks_dir.mkdir(parents=True, exist_ok=True)
    if path.exists():
        remove_worktree(repo, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            f"core.hooksPath={hooks_dir}",
            "worktree",
            "add",
            "--detach",
            "--quiet",
            str(path),
            sha,
        ],
        env={"GIT_LFS_SKIP_SMUDGE": "1"},
    )


def set_writable(path: Path, writable: bool) -> None:
    """Toggle write permission on a worktree's files (agents only ever read worktrees)."""
    for root, dirs, files in os.walk(path):
        if ".git" in Path(root).parts:
            continue
        for name in dirs + files:
            target = Path(root) / name
            if target.is_symlink() or name == ".git":
                continue
            mode = target.stat().st_mode
            target.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IWUSR if writable else mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


def remove_worktree(repo: Path, path: Path) -> None:
    if path.exists():
        set_writable(path, True)
    run(["git", "-C", str(repo), "worktree", "remove", "--force", str(path)], check=False)
    run(["git", "-C", str(repo), "worktree", "prune"], check=False)


def worktree_dirty(path: Path) -> str:
    return git(path, "status", "--porcelain", "--untracked-files=all")


def reset_worktree(path: Path, sha: str) -> None:
    set_writable(path, True)
    git(path, "checkout", "--quiet", "--force", "--detach", sha)
    git(path, "clean", "-ffdxq")


def diff_names(repo: Path, base: str, head: str) -> list[str]:
    out = git(repo, "diff", "--name-only", "--no-renames", "-z", base, head)
    return [p for p in out.split("\0") if p]
