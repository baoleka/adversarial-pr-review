"""Triage (no LLM): measure the diff, classify files, compute risk, pick lanes and review units."""

from __future__ import annotations

import ast
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from diffs import FileDiff

from common import git, git_ok, glob_match

CHANGE_TYPES = {
    "refactor": re.compile(
        r"\b(refactor|rename|restructur|simplif|clean ?up|no behaviou?r change|mechanical)\w*", re.I
    ),
    "feature": re.compile(r"\b(feat|feature|add|adds|new|support|introduce|implement)\w*", re.I),
    "fix": re.compile(r"\b(fix|bug|regression|crash|correct|handle|prevent)\w*", re.I),
    "dependency": re.compile(r"\b(bump|upgrade|dependenc|deps|requirements|pin)\w*", re.I),
}


@dataclass
class Unit:
    id: str
    files: list[str]
    changed_lines: int
    risk: float


@dataclass
class Triage:
    changed_files: list[str]
    changed_lines: int
    reviewable_lines: int
    excluded_files: list[str]
    classes: dict[str, list[str]]
    change_types: list[str]
    comment_only: bool
    docs_only: bool
    public_interface_changes: list[str]
    sensitive_files: list[str]
    security_hits: list[str]
    injection_hits: list[str]
    dependency_change: bool
    risk: float
    size: str
    lanes: list[str]
    units: list[Unit] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["units"] = [asdict(u) for u in self.units]
        return data


def classify(paths: Iterable[str], file_classes: Mapping[str, list[str]]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {name: [] for name in file_classes}
    for path in paths:
        for name, globs in file_classes.items():
            if glob_match(path, globs):
                out[name].append(path)
    return out


def _strip_docstrings(tree: ast.AST) -> ast.AST:
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)
            ):
                node.body = body[1:] or [ast.Pass()]
    return tree


def ast_equal_without_docstrings(old: str, new: str) -> bool:
    try:
        a = _strip_docstrings(ast.parse(old))
        b = _strip_docstrings(ast.parse(new))
    except SyntaxError:
        return False
    return ast.dump(a, include_attributes=False) == ast.dump(b, include_attributes=False)


def file_at(repo: Path, rev: str, path: str) -> str:
    return git(repo, "show", f"{rev}:{path}") if git_ok(repo, "cat-file", "-e", f"{rev}:{path}") else ""


def public_api(source: str) -> dict[str, str]:
    """Public names and their signatures (functions, classes, methods, __all__)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {}
    api: dict[str, str] = {}

    def signature(fn: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
        return f"({ast.unparse(fn.args)})" + (f" -> {ast.unparse(fn.returns)}" if fn.returns else "")

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            api[node.name] = signature(node)
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            api[node.name] = "class(" + ", ".join(ast.unparse(b) for b in node.bases) + ")"
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and (
                    not item.name.startswith("_") or item.name == "__init__"
                ):
                    api[f"{node.name}.{item.name}"] = signature(item)
        elif isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "__all__" for t in node.targets):
            api["__all__"] = ast.unparse(node.value)
    return api


def interface_changes(repo: Path, base: str, head: str, paths: Iterable[str]) -> list[str]:
    changes = []
    for path in paths:
        if not path.endswith(".py"):
            continue
        old, new = public_api(file_at(repo, base, path)), public_api(file_at(repo, head, path))
        for name in sorted(set(old) | set(new)):
            if old.get(name) != new.get(name):
                status = "added" if name not in old else "removed" if name not in new else "changed"
                changes.append(f"{path}:{name} ({status})")
    return changes


def change_types(intent: str) -> list[str]:
    found = [name for name, regex in CHANGE_TYPES.items() if regex.search(intent or "")]
    return found or sorted(CHANGE_TYPES)


def module_key(path: str) -> str:
    parts = Path(path).parts
    if len(parts) >= 3 and parts[0] in ("src", "lib"):
        return "/".join(parts[:3]) if not parts[2].endswith(".py") else f"{parts[0]}/{parts[1]}/{Path(parts[2]).stem}"
    if len(parts) >= 2:
        return f"{parts[0]}/{Path(parts[1]).stem}" if parts[1].endswith(".py") else "/".join(parts[:2])
    return Path(path).stem or path


def test_target(path: str) -> str | None:
    """tests/test_foo.py -> 'foo', so the test lands in the same unit as foo.py."""
    name = Path(path).name
    if name.startswith("test_") and name.endswith(".py"):
        return name[5:-3]
    return None


def partition(
    file_diffs: Mapping[str, FileDiff], reviewable: list[str], cap: int, risk_of: Mapping[str, float]
) -> list[Unit]:
    groups: dict[str, list[str]] = {}
    for path in reviewable:
        target = test_target(path)
        key = module_key(path)
        if target:
            owner = next((p for p in reviewable if Path(p).stem == target and not test_target(p)), None)
            key = module_key(owner) if owner else key
        groups.setdefault(key, []).append(path)
    # 1. Split each module group (tests with their sources) into pieces of at most `cap` lines.
    pieces: list[list[str]] = []
    for key in sorted(groups):
        bucket: list[str] = []
        size = 0
        for path in sorted(groups[key], key=lambda p: (-file_diffs[p].changed_lines, p)):
            lines = file_diffs[path].changed_lines
            if bucket and size + lines > cap:
                pieces.append(bucket)
                bucket, size = [], 0
            bucket.append(path)
            size += lines
        if bucket:
            pieces.append(bucket)

    # 2. Pack whole pieces into units of at most `cap` lines (first fit, largest first), so many
    #    small modules don't each cost a full set of lanes.
    def piece_size(piece: list[str]) -> int:
        return sum(file_diffs[p].changed_lines for p in piece)

    bins: list[list[str]] = []
    for piece in sorted(pieces, key=lambda pc: (-piece_size(pc), pc[0])):
        fit = next((b for b in bins if piece_size(b) + piece_size(piece) <= cap), None)
        if fit is None:
            bins.append(list(piece))
        else:
            fit.extend(piece)
    units = [Unit("", sorted(b), piece_size(b), sum(risk_of.get(p, 0) for p in b)) for b in bins]
    units.sort(key=lambda u: (-u.risk, -u.changed_lines, u.files[0]))
    for index, unit in enumerate(units, start=1):
        unit.id = f"u{index}"
    return units


def run_triage(
    repo: Path, cfg: Mapping[str, Any], base: str, head: str, file_diffs: Mapping[str, FileDiff], intent: str
) -> Triage:
    repo_cfg = cfg["repo"]
    limits = cfg["limits"]
    classes = classify(file_diffs, repo_cfg.get("file_classes", {}))
    excluded = sorted(set(classes.get("generated", []) + classes.get("vendored", []) + classes.get("lockfiles", [])))
    changed_lines = sum(fd.changed_lines for fd in file_diffs.values())
    reviewable = sorted(p for p in file_diffs if p not in excluded and file_diffs[p].status != "binary")
    reviewable_lines = sum(file_diffs[p].changed_lines for p in reviewable)
    docs = set(classes.get("docs", [])) | set(classes.get("changelogs", []))
    docs_only = bool(file_diffs) and all(p in docs for p in file_diffs)
    comment_only = bool(file_diffs) and all(
        p in docs
        or (
            p.endswith(".py")
            and file_diffs[p].status == "modified"
            and ast_equal_without_docstrings(file_at(repo, base, p), file_at(repo, head, p))
        )
        for p in file_diffs
    )
    public = interface_changes(
        repo, base, head, [p for p in reviewable if glob_match(p, repo_cfg["file_classes"].get("public_interface", []))]
    )
    sensitive = sorted(p for p in file_diffs if glob_match(p, repo_cfg.get("sensitive_paths", [])))
    hits = []
    patterns = [re.compile(p) for p in repo_cfg.get("security_patterns", [])]
    for path, fd in sorted(file_diffs.items()):
        for line, text in list(fd.added.items()) + list(fd.removed.items()):
            if any(p.search(text[:2000]) for p in patterns):
                hits.append(f"{path}:{line}")
    injection = [re.compile(p) for p in repo_cfg.get("injection_patterns", [])]
    injection_hits = []
    for path, fd in sorted(file_diffs.items()):
        for line, text in sorted(fd.added.items()):
            if any(p.search(text[:2000]) for p in injection):
                injection_hits.append(f"{path}:{line}")
    dependency_change = bool(classes.get("dependency_manifests") or classes.get("lockfiles"))
    risk_of = {p: 1.0 + 3.0 * (p in sensitive) + 2.0 * any(c.startswith(p + ":") for c in public) for p in reviewable}
    risk = (
        math.ceil(math.log2(reviewable_lines + 1))
        + 3 * len(sensitive)
        + 2 * len(public)
        + 2 * dependency_change
        + (len(reviewable) > limits["size_thresholds"]["max_files"])
    )
    thresholds = limits["size_thresholds"]
    if reviewable_lines > thresholds["medium_lines"] or len(reviewable) > thresholds["max_files"]:
        size = "large"
    elif reviewable_lines >= thresholds["small_lines"]:
        size = "medium"
    else:
        size = "small"
    lanes: list[str] = []
    if not comment_only and reviewable_lines > 0:
        lanes = ["breaker", "external"]
        tests_changed = bool(classes.get("tests"))
        if size in ("medium", "large"):
            lanes.append("contracts")
            if tests_changed or size == "large":
                lanes.append("tests")
        if size == "large":
            lanes.append("security")
        if public and "contracts" not in lanes:
            lanes.append("contracts")
        if (sensitive or hits or injection_hits) and "security" not in lanes:
            lanes.append("security")
        lanes = [
            lane
            for lane in ("breaker", "external", "contracts", "tests", "security")
            if lane in lanes and cfg["lanes"].get(lane, {}).get("enabled", True)
        ]
    units: list[Unit] = []
    if lanes:
        # Docs and changelogs never form a unit of their own; they ride along with the
        # highest-risk code unit as context.
        code = [p for p in reviewable if p not in docs]
        units = partition(file_diffs, code, int(limits["unit_size_cap"]), risk_of)
        doc_files = sorted(p for p in reviewable if p in docs)
        if units and doc_files:
            units[0].files = sorted(units[0].files + doc_files)
    return Triage(
        changed_files=sorted(file_diffs),
        changed_lines=changed_lines,
        reviewable_lines=reviewable_lines,
        excluded_files=excluded,
        classes={k: v for k, v in classes.items() if v},
        change_types=change_types(intent),
        comment_only=comment_only,
        docs_only=docs_only,
        public_interface_changes=public,
        sensitive_files=sensitive,
        security_hits=hits[:200],
        injection_hits=injection_hits[:50],
        dependency_change=dependency_change,
        risk=float(risk),
        size=size,
        lanes=lanes,
        units=units,
    )
