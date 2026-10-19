"""Mechanical checks on the change: suppressions, changelogs, tests that run, diff coverage and
review integrity. Every check finding is origin `check`, confidence certain.
"""

from __future__ import annotations

import ast
import re
import sys
import xml.etree.ElementTree as ET
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

import findings as fmod
import parsers
from diffs import FileDiff

from common import SKILL_DIR, git, git_ok, glob_match, run

BARE_TYPE_IGNORE = re.compile(r"#\s*type:\s*ignore(?!\[)")
BLANKET_NOQA = re.compile(r"#\s*noqa(?!\s*:)", re.IGNORECASE)
TEST_FUNC = re.compile(r"test", re.IGNORECASE)
SKIP_MARK = re.compile(r"(pytest\.mark\.(skip|skipif|xfail)\b|pytest\.(skip|xfail)\(|unittest\.skip)")
# The skill's own files, under its development name, its production name and the name it was
# installed under: a change that edits them edits the review itself.
SKILL_INPUTS = tuple(
    dict.fromkeys(
        [f".claude/skills/{name}/**" for name in ("adversarial-review-v2", "adversarial-pr-review", SKILL_DIR.name)]
        + [".claude/agents/review-*", ".claude/settings*"]
    )
)


def sev(cfg: Mapping[str, Any], key: str, default: str) -> str:
    return str(cfg["repo"].get("severity_map", {}).get(key, default))


def matches(path: str, globs: Iterable[str]) -> bool:
    return glob_match(path, globs)


# ---------------------------------------------------------------- review integrity


def _tool_tables(text: str, tables: Iterable[str]) -> dict[str, Any] | None:
    """The configured tool tables of a TOML file; None when tomllib is unavailable (< 3.11)."""
    if sys.version_info >= (3, 11):
        import tomllib

        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError:
            return {"__invalid__": text}
        out: dict[str, Any] = {}
        for dotted in tables:
            node: Any = data
            for part in dotted.split("."):
                node = node.get(part) if isinstance(node, dict) else None
            out[dotted] = node
        return out
    return None


def tool_tables_differ(repo: Path, cfg: Mapping[str, Any], base: str, head: str, rel: str) -> bool:
    def text_at(rev: str) -> str:
        return git(repo, "show", f"{rev}:{rel}") if git_ok(repo, "cat-file", "-e", f"{rev}:{rel}") else ""

    old, new = text_at(base), text_at(head)
    tables = cfg["repo"].get("tool_tables", [])
    a, b = _tool_tables(old, tables), _tool_tables(new, tables)
    return old != new if a is None or b is None else a != b


def review_integrity_findings(
    repo: Path,
    cfg: Mapping[str, Any],
    base: str,
    head: str,
    changed: list[str],
    config_source: str,
    round_no: int,
    acked: set[str],
) -> list[dict[str, Any]]:
    """A change that touches its own review inputs gets a major review-integrity finding."""
    hits: list[tuple[str, str]] = []
    globs = list(cfg["repo"].get("review_inputs", [])) + list(SKILL_INPUTS)
    for path in changed:
        if path == "pyproject.toml":
            if tool_tables_differ(repo, cfg, base, head, path):
                hits.append((path, "tool configuration tables changed"))
        elif matches(path, globs):
            hits.append((path, "review input changed"))
    if config_source == "head":
        hits.append(("review.config.json", "no review config at the merge-base; the head copy was used"))
    out = []
    for path, why in hits:
        finding = fmod.make(
            origin="check",
            category="review-integrity",
            severity=sev(cfg, "check:review-integrity", "major"),
            title=f"Change touches a review input: {path}",
            file=path,
            start_line=1,
            kind="omission",
            anchor_text=f"review-integrity {path}",
            conditions="The diff modifies configuration, tooling or baselines that the review itself relies on.",
            expected="Review inputs are read from the merge-base and are not changed by the change under review.",
            actual=f"{path}: {why}. Only the user can acknowledge this with --ack <finding id>.",
            round_no=round_no,
            raw_ref="check:review-integrity",
        )
        if finding["id"] in acked:
            finding["status"] = "acknowledged"
        out.append(finding)
    return out


# ---------------------------------------------------------------- suppressions


def added_line_suppressions(
    file_diffs: Mapping[str, FileDiff], cfg: Mapping[str, Any], round_no: int
) -> list[dict[str, Any]]:
    """Bare `# type: ignore` and blanket `# noqa` on added lines (text check; always available)."""
    out = []
    for path, fd in sorted(file_diffs.items()):
        if not path.endswith(".py"):
            continue
        for line, text in sorted(fd.added.items()):
            for regex, key, label in (
                (BARE_TYPE_IGNORE, "check:ignore-bare", "bare `# type: ignore` without an error code"),
                (BLANKET_NOQA, "check:ignore-blanket", "blanket `# noqa` without a rule code"),
            ):
                if regex.search(text):
                    out.append(
                        fmod.make(
                            origin="check",
                            category="suppression",
                            severity=sev(cfg, key, "major"),
                            title=f"Added {label} in {path}",
                            file=path,
                            start_line=line,
                            quote=text.strip(),
                            anchor_text=f"{key} {text.strip()}",
                            conditions="A suppression comment is added by this change.",
                            expected="Suppressions name the exact error code(s) they silence.",
                            actual=f"Line {line} adds a {label}, which can hide unrelated errors.",
                            suggested_fix="Name the exact code, for example `# type: ignore[attr-defined]` or `# noqa: E501`, or fix the underlying error.",
                            round_no=round_no,
                            raw_ref=f"check:{key}",
                        )
                    )
    return out


def tool_suppression_findings(
    diags: list[parsers.Diagnostic], file_diffs: Mapping[str, FileDiff], cfg: Mapping[str, Any], round_no: int
) -> list[dict[str, Any]]:
    """Tool-reported suppression problems (ruff PGH003/PGH004/RUF100, mypy ignore-without-code and
    unused-ignore) kept only when they sit on added lines."""
    out = []
    relevant = {
        "PGH003": "check:ignore-bare",
        "PGH004": "check:ignore-blanket",
        "RUF100": "check:ignore-unused",
        "ignore-without-code": "check:ignore-bare",
        "unused-ignore": "check:ignore-unused",
    }
    for d in diags:
        key = relevant.get(d.rule)
        fd = file_diffs.get(d.file)
        if key is None or fd is None or d.line not in fd.added:
            continue
        out.append(
            fmod.make(
                origin="check",
                category="suppression",
                severity=sev(cfg, key, "major" if key != "check:ignore-unused" else "minor"),
                title=f"{d.tool} {d.rule} on an added line in {d.file}",
                file=d.file,
                start_line=d.line,
                quote=fd.added[d.line].strip() or None,
                symbol=d.symbol if d.symbol != "<module>" else None,
                anchor_text=f"{key} {fd.added[d.line].strip()}",
                conditions="Running the linters and type checker with suppression checks enabled.",
                expected="Suppressions on added lines are exact and needed.",
                actual=d.message,
                observed_output=f"{d.file}:{d.line}: [{d.rule}] {d.message}",
                round_no=round_no,
                raw_ref=f"check:{d.rule}",
            )
        )
    return out


def dedupe(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: dict[tuple[str, int, str], dict[str, Any]] = {}
    for f in items:
        key = (f["location"]["file"], f["location"]["start_line"], f["category"])
        if key not in seen or fmod.SEVERITY_RANK[f["severity"]] < fmod.SEVERITY_RANK[seen[key]["severity"]]:
            seen[key] = f
    return sorted(seen.values(), key=fmod.sort_key)


# ---------------------------------------------------------------- changelogs


def changelog_findings(
    repo: Path,
    cfg: Mapping[str, Any],
    base: str,
    file_diffs: Mapping[str, FileDiff],
    messages: str,
    intent: str,
    round_no: int,
) -> list[dict[str, Any]]:
    out = []
    changed = list(file_diffs)
    for rule in cfg["repo"].get("changelogs", []):
        touched = [p for p in changed if matches(p, rule["globs"])]
        if not touched:
            continue
        marker = rule["skip_marker"]
        if marker in messages or marker in intent:
            continue
        fd = file_diffs.get(rule["path"])
        section = re.compile(rule.get("section_pattern") or r"^#+ ")
        if fd is None or not fd.added:
            out.append(
                fmod.make(
                    origin="check",
                    category="changelog",
                    severity=sev(cfg, "check:changelog-missing", "major"),
                    title=f"Missing {rule['path']} entry",
                    file=rule["path"],
                    start_line=1,
                    kind="omission",
                    anchor_text=f"changelog-missing {rule['path']}",
                    conditions=f"The change touches {', '.join(touched[:5])}{' …' if len(touched) > 5 else ''}.",
                    expected=f"An entry is added to {rule['path']} (or the `{marker}` marker is used).",
                    actual=f"{rule['path']} has no added lines.",
                    round_no=round_no,
                    raw_ref="check:changelog",
                )
            )
            continue
        text = (
            git(repo, "show", f"{base}:{rule['path']}")
            if git_ok(repo, "cat-file", "-e", f"{base}:{rule['path']}")
            else ""
        )
        if not _added_under_section(text, fd, section):
            out.append(
                fmod.make(
                    origin="check",
                    category="changelog",
                    severity=sev(cfg, "check:changelog-misplaced", "minor"),
                    title=f"{rule['path']} entry is outside the expected section",
                    file=rule["path"],
                    start_line=min(fd.added),
                    quote=fd.added[min(fd.added)].strip() or None,
                    anchor_text=f"changelog-misplaced {rule['path']}",
                    conditions=f"The change adds lines to {rule['path']}.",
                    expected=f"New entries go under the section matching `{section.pattern}`.",
                    actual="The added lines are not under that section.",
                    round_no=round_no,
                    raw_ref="check:changelog",
                )
            )
    return out


def _added_under_section(base_text: str, fd: FileDiff, section: re.Pattern[str]) -> bool:
    """True if some added line falls under a heading that matches ``section`` at head."""
    head_lines: dict[int, str] = {}
    base_lines = base_text.splitlines()
    removed = set(fd.removed)
    # Rebuild head line numbers from the base file plus the diff's added lines.
    bi, hi = 1, 1
    while bi <= len(base_lines) or hi in fd.added:
        if hi in fd.added:
            head_lines[hi] = fd.added[hi]
            hi += 1
            continue
        if bi in removed:
            bi += 1
            continue
        head_lines[hi] = base_lines[bi - 1]
        bi += 1
        hi += 1
    current_ok = False
    for number in sorted(head_lines):
        text = head_lines[number]
        if text.startswith("#"):
            current_ok = bool(section.search(text))
            continue
        if number in fd.added and text.strip() and current_ok:
            return True
    return False


# ---------------------------------------------------------------- tests that run


def _test_functions(source: str) -> list[tuple[str, int, int, bool]]:
    """(qualified name, start, end, is_collectable_name) for test-like functions."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    out = []

    def is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
        return any("fixture" in ast.unparse(d) for d in node.decorator_list)

    def has_assert(node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Assert) or (isinstance(n, ast.Call) and "assert" in ast.unparse(n.func).lower())
            for n in ast.walk(node)
        )

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if node.name.startswith("_") or is_fixture(node):
                continue
            if TEST_FUNC.search(node.name) or has_assert(node):
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                out.append((node.name, start, node.end_lineno or node.lineno, node.name.startswith("test")))
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and not item.name.startswith("_")
                    and not is_fixture(item)
                ):
                    if TEST_FUNC.search(item.name) or has_assert(item):
                        start = min([item.lineno] + [d.lineno for d in item.decorator_list])
                        out.append(
                            (
                                f"{node.name}::{item.name}",
                                start,
                                item.end_lineno or item.lineno,
                                item.name.startswith("test"),
                            )
                        )
    return out


def tests_run_findings(
    cfg: Mapping[str, Any],
    head_root: Path,
    file_diffs: Mapping[str, FileDiff],
    report: parsers.PytestReport | None,
    round_no: int,
) -> list[dict[str, Any]]:
    settings = cfg["repo"]["checks"].get("tests_run", {})
    if not settings.get("enabled") or report is None:
        return []
    globs = settings.get("test_globs") or ["tests/**/test_*.py", "test_*.py", "**/test_*.py"]
    collected = set(report.collected)
    out = []
    for path, fd in sorted(file_diffs.items()):
        if not path.endswith(".py") or fd.status == "deleted" or not fd.added:
            continue
        is_test_file = matches(path, globs) or Path(path).name.startswith("test_") or "/tests/" in f"/{path}"
        if not is_test_file:
            continue
        source = (
            (head_root / path).read_text(encoding="utf-8", errors="replace") if (head_root / path).is_file() else ""
        )
        ids_in_file = [n for n in collected if n.split("::", 1)[0] == path]
        if fd.status == "added" and not ids_in_file and matches(path, globs):
            out.append(
                _test_finding(
                    cfg,
                    "check:test-uncollected",
                    path,
                    1,
                    None,
                    "New test file collects no tests",
                    "pytest collects at least one test from a new test file.",
                    "pytest collected nothing from this file.",
                    round_no,
                )
            )
            continue
        for name, start, end, _ in _test_functions(source):
            touched = any(start <= line <= end for line in fd.added)
            if not touched:
                continue
            prefix = f"{path}::{name}"
            matched = [n for n in collected if n == prefix or n.startswith(prefix + "[")]
            if not matched:
                out.append(
                    _test_finding(
                        cfg,
                        "check:test-uncollected",
                        path,
                        start,
                        name,
                        f"Test `{name}` is never collected",
                        "Every test added or modified by the change is collected by pytest.",
                        f"pytest does not collect `{name}` (check its name, class and file).",
                        round_no,
                        quote=source.splitlines()[start - 1].strip() if source else None,
                    )
                )
                continue
            outcomes = [report.outcomes.get(n, {}).get("outcome") for n in matched]
            if all(o in ("skipped", None) for o in outcomes):
                out.append(
                    _test_finding(
                        cfg,
                        "check:test-unexecuted",
                        path,
                        start,
                        name,
                        f"Test `{name}` is collected but never runs",
                        "Tests added or modified by the change execute.",
                        f"Outcome(s): {', '.join(str(o) for o in outcomes)}.",
                        round_no,
                        quote=source.splitlines()[start - 1].strip() if source else None,
                    )
                )
        for line, text in sorted(fd.added.items()):
            if SKIP_MARK.search(text):
                out.append(
                    _test_finding(
                        cfg,
                        "check:test-new-skip",
                        path,
                        line,
                        None,
                        "New skip or xfail marker",
                        "New tests run unconditionally unless the skip is justified.",
                        f"Line {line} adds `{text.strip()[:120]}`.",
                        round_no,
                        quote=text.strip(),
                    )
                )
    return out


def _test_finding(
    cfg: Mapping[str, Any],
    key: str,
    path: str,
    line: int,
    symbol: str | None,
    title: str,
    expected: str,
    actual: str,
    round_no: int,
    quote: str | None = None,
) -> dict[str, Any]:
    default = "minor" if key == "check:test-new-skip" else "major"
    return fmod.make(
        origin="check",
        category="test-not-run",
        severity=sev(cfg, key, default),
        title=title,
        file=path,
        start_line=line,
        quote=quote,
        symbol=symbol,
        anchor_text=f"{key} {path}::{symbol or line}",
        conditions="Collecting and running the test suite at head with the review's pytest plugin.",
        expected=expected,
        actual=actual,
        round_no=round_no,
        raw_ref=f"check:{key}",
    )


# ---------------------------------------------------------------- diff coverage


def executable_lines(source: str) -> set[int]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return set()
    lines: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.stmt) and not isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Import, ast.ImportFrom)
        ):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                continue
            lines.add(node.lineno)
    return lines


def covered_lines(coverage_xml: str, root: Path) -> dict[str, set[int]]:
    tree = ET.parse(coverage_xml)
    sources = [s.text or "" for s in tree.getroot().iter("source")]
    out: dict[str, set[int]] = {}
    for cls in tree.getroot().iter("class"):
        filename = cls.get("filename", "")
        rel = filename
        for src in sources:
            candidate = Path(src) / filename
            try:
                rel = candidate.resolve().relative_to(root.resolve()).as_posix()
                break
            except ValueError:
                continue
        hits = out.setdefault(rel, set())
        for line in cls.iter("line"):
            if int(line.get("hits", "0")) > 0:
                hits.add(int(line.get("number", "0")))
    return out


def diff_coverage_finding(
    cfg: Mapping[str, Any], head_root: Path, file_diffs: Mapping[str, FileDiff], coverage_xml: str | None, round_no: int
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    settings = cfg["repo"]["checks"].get("diff_coverage", {})
    if not settings.get("enabled") or not coverage_xml:
        return None, {"enabled": bool(settings.get("enabled")), "measured": False}
    covered = covered_lines(coverage_xml, head_root)
    test_globs = cfg["repo"]["file_classes"].get("tests", [])
    total = hit = 0
    missed: list[str] = []
    for path, fd in sorted(file_diffs.items()):
        if not path.endswith(".py") or matches(path, test_globs) or path not in covered:
            continue
        source = (head_root / path).read_text(encoding="utf-8", errors="replace")
        relevant = executable_lines(source) & set(fd.added)
        total += len(relevant)
        hit += len(relevant & covered[path])
        missed += [f"{path}:{n}" for n in sorted(relevant - covered[path])][:20]
    ratio = 1.0 if total == 0 else hit / total
    summary = {
        "enabled": True,
        "measured": True,
        "changed_executable_lines": total,
        "covered": hit,
        "ratio": round(ratio, 4),
    }
    threshold = float(cfg["repo"].get("diff_coverage_threshold", 0.8))
    if total == 0 or ratio >= threshold:
        return None, summary
    first = missed[0].split(":")[0] if missed else "?"
    return (
        fmod.make(
            origin="check",
            category="coverage",
            severity=sev(cfg, "check:diff-coverage", "minor"),
            title=f"Diff coverage {ratio:.0%} is below {threshold:.0%}",
            file=first,
            start_line=int(missed[0].split(":")[1]) if missed else 1,
            anchor_text="diff-coverage",
            kind="omission",
            conditions="Running the test suite with coverage at head.",
            expected=f"At least {threshold:.0%} of the changed executable lines are covered.",
            actual=f"{hit} of {total} changed executable lines are covered. Uncovered: {', '.join(missed[:10])}",
            round_no=round_no,
            raw_ref="check:diff-coverage",
        ),
        summary,
    )


def skill_dir_rel(repo: Path) -> str | None:
    try:
        return SKILL_DIR.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return None


def ruff_version_ok(env: Mapping[str, str]) -> bool:
    return run(["ruff", "--version"], env=env, check=False).returncode == 0
