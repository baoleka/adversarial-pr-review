"""Tool-output parsers, diagnostic keys, message masking and the base/head multiset diff."""

from __future__ import annotations

import ast
import json
import re
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

MAX_MESSAGE = 4000
_DIGITS = re.compile(r"\d+")
_HEX = re.compile(r"0x[0-9a-fA-F]+")
SUPPRESSION_MARKERS = ("type: ignore", "noqa", "pragma: no cover", "pyright: ignore")


@dataclass(frozen=True)
class Diagnostic:
    tool: str
    rule: str
    file: str
    line: int
    message: str
    symbol: str = "<module>"
    extra: tuple[tuple[str, str], ...] = field(default_factory=tuple)

    @property
    def key(self) -> tuple[str, str, str, str, str]:
        return (self.tool, self.rule, self.file, self.symbol, mask(self.message))

    @property
    def loose_key(self) -> tuple[str, str, str, str]:
        return (self.tool, self.rule, self.file, self.symbol)

    def to_json(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "rule": self.rule,
            "file": self.file,
            "line": self.line,
            "message": self.message,
            "symbol": self.symbol,
        }


def mask(message: str) -> str:
    """Mask numbers, hex addresses and paths so keys survive unrelated line shifts.

    Token-based and linear-time: no regex here can backtrack catastrophically on hostile input.
    """
    tokens = []
    for token in message[:MAX_MESSAGE].split():
        if "/" in token or "\\" in token:
            tokens.append("<path>")
        else:
            tokens.append(_DIGITS.sub("<n>", _HEX.sub("<hex>", token)))
    return " ".join(tokens)


def _rel(path: str, root: Path) -> str:
    candidate = Path(path)
    if candidate.is_absolute():
        try:
            return candidate.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            return candidate.as_posix()
    return candidate.as_posix().removeprefix("./")


def _int(value: Any, default: int = 1) -> int:
    """Lines must be positive integers; anything else falls back to ``default`` (fail closed)."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        return default
    return int(value)


def parse_ruff_json(text: str, root: Path) -> list[Diagnostic]:
    data = json.loads(text or "[]")
    if not isinstance(data, list):
        raise ValueError("ruff JSON output is not a list")
    out = []
    for item in data:
        if not isinstance(item, dict) or not isinstance(item.get("filename"), str):
            raise ValueError("malformed ruff diagnostic")
        loc = item.get("location") or {}
        out.append(
            Diagnostic(
                "ruff",
                str(item.get("code") or "syntax"),
                _rel(item["filename"], root),
                _int(loc.get("row")),
                str(item.get("message", "")),
            )
        )
    return out


def parse_ruff_format(text: str, root: Path) -> list[Diagnostic]:
    out = []
    for line in text.splitlines():
        if line.startswith("Would reformat: "):
            out.append(
                Diagnostic(
                    "ruff-format",
                    "format",
                    _rel(line[len("Would reformat: ") :].strip(), root),
                    1,
                    "file would be reformatted",
                )
            )
    return out


def parse_mypy_json(text: str, root: Path) -> list[Diagnostic]:
    out = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        item = json.loads(line)
        if not isinstance(item, dict) or not isinstance(item.get("file"), str):
            raise ValueError("malformed mypy diagnostic")
        if item.get("severity") != "error":
            continue
        out.append(
            Diagnostic(
                "mypy",
                str(item.get("code") or "misc"),
                _rel(item["file"], root),
                _int(item.get("line")),
                str(item.get("message", "")),
            )
        )
    return out


MYPY_TEXT = re.compile(
    r"^(?P<file>[^:\n]{1,400}):(?P<line>\d{1,7}):(?:\d{1,7}:)? error: (?P<msg>.*?)(?:  \[(?P<code>[a-z0-9-]{1,60})\])?$"
)


def parse_mypy_text(text: str, root: Path) -> list[Diagnostic]:
    out = []
    for line in text.splitlines():
        match = MYPY_TEXT.match(line[:MAX_MESSAGE])
        if match:
            out.append(
                Diagnostic(
                    "mypy",
                    match.group("code") or "misc",
                    _rel(match.group("file"), root),
                    int(match.group("line")),
                    match.group("msg"),
                )
            )
    return out


@dataclass
class PytestReport:
    collected: list[str]
    outcomes: dict[str, dict[str, Any]]
    exitstatus: int | None


def parse_pytest_report(text: str) -> PytestReport:
    data = json.loads(text)
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("collected"), list)
        or not isinstance(data.get("outcomes"), dict)
    ):
        raise ValueError("malformed pytest report")
    outcomes = {}
    for node, entry in data["outcomes"].items():
        if not isinstance(node, str) or not isinstance(entry, dict) or not isinstance(entry.get("file"), str):
            raise ValueError("malformed pytest outcome")
        outcomes[node] = {
            "outcome": str(entry.get("outcome", "error")),
            "file": entry["file"],
            "line": _int(entry.get("line")),
            "message": str(entry.get("message", ""))[:MAX_MESSAGE],
        }
    status = data.get("exitstatus")
    return PytestReport([str(n) for n in data["collected"]], outcomes, status if isinstance(status, int) else None)


def pytest_failures(report: PytestReport) -> list[Diagnostic]:
    return [
        Diagnostic(
            "pytest",
            entry["outcome"],
            entry["file"],
            entry["line"],
            f"{node}: {entry['message']}",
            extra=(("node", node),),
        )
        for node, entry in sorted(report.outcomes.items())
        if entry["outcome"] in ("failed", "error")
    ]


def parse_regex(text: str, root: Path, pattern: str, tool: str) -> list[Diagnostic]:
    regex = re.compile(pattern)
    out = []
    for line in text.splitlines():
        match = regex.match(line[:MAX_MESSAGE])
        if match:
            groups = match.groupdict()
            out.append(
                Diagnostic(
                    tool,
                    groups.get("rule") or tool,
                    _rel(groups.get("file") or "", root),
                    _int(int(groups["line"]) if (groups.get("line") or "").isdigit() else 1),
                    groups.get("message") or line,
                )
            )
    return out


# ---------------------------------------------------------------- enclosing symbols


def symbol_map(source: str) -> list[tuple[int, int, str]]:
    """(start, end, qualified name) for every function and class, innermost last."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    spans: list[tuple[int, int, str]] = []

    def walk(nodes: Iterable[ast.AST], prefix: str) -> None:
        for node in nodes:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                name = f"{prefix}{node.name}"
                start = min([node.lineno] + [d.lineno for d in node.decorator_list])
                spans.append((start, node.end_lineno or node.lineno, name))
                walk(node.body, name + ".")
            elif hasattr(node, "body") and isinstance(node, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                walk(getattr(node, "body", []), prefix)

    walk(tree.body, "")
    return spans


def enclosing_symbol(spans: list[tuple[int, int, str]], line: int) -> str:
    best = "<module>"
    best_size = None
    for start, end, name in spans:
        if start <= line <= end and (best_size is None or end - start <= best_size):
            best, best_size = name, end - start
    return best


def with_symbols(diags: list[Diagnostic], root: Path) -> list[Diagnostic]:
    cache: dict[str, list[tuple[int, int, str]]] = {}
    out = []
    for d in diags:
        if d.file.endswith(".py"):
            if d.file not in cache:
                path = root / d.file
                cache[d.file] = symbol_map(path.read_text(encoding="utf-8", errors="replace")) if path.is_file() else []
            out.append(
                Diagnostic(d.tool, d.rule, d.file, d.line, d.message, enclosing_symbol(cache[d.file], d.line), d.extra)
            )
        else:
            out.append(d)
    return out


# ---------------------------------------------------------------- base/head diff


@dataclass
class DiagnosticDiff:
    new: list[Diagnostic] = field(default_factory=list)
    changed: list[tuple[Diagnostic, Diagnostic]] = field(default_factory=list)  # (base, head)
    fixed: list[Diagnostic] = field(default_factory=list)
    suppressed: list[Diagnostic] = field(default_factory=list)
    persisting: list[Diagnostic] = field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {k: len(getattr(self, k)) for k in ("new", "changed", "fixed", "suppressed", "persisting")}


def diff_diagnostics(
    base: list[Diagnostic],
    head: list[Diagnostic],
    anchor_change: Callable[[Diagnostic], str],
) -> DiagnosticDiff:
    """Compare keys as multisets: only the excess at head is new.

    ``anchor_change(d)`` returns "unchanged", "suppression" (the anchor line only gained a
    suppression comment) or "changed" for a base diagnostic's anchor line.
    """
    out = DiagnosticDiff()
    base_counts = Counter(d.key for d in base)
    head_counts = Counter(d.key for d in head)
    unmatched_head: list[Diagnostic] = []
    for key in sorted(head_counts):
        instances = sorted((d for d in head if d.key == key), key=lambda d: d.line)
        keep = min(head_counts[key], base_counts.get(key, 0))
        out.persisting.extend(instances[:keep])
        unmatched_head.extend(instances[keep:])
    unmatched_base: list[Diagnostic] = []
    for key in sorted(base_counts):
        excess = base_counts[key] - head_counts.get(key, 0)
        if excess > 0:
            unmatched_base.extend(sorted((d for d in base if d.key == key), key=lambda d: d.line)[-excess:])
    remaining_base = list(unmatched_base)
    for h in unmatched_head:
        partner = next((b for b in remaining_base if b.loose_key == h.loose_key), None)
        if partner is not None:
            remaining_base.remove(partner)
            out.changed.append((partner, h))
        else:
            out.new.append(h)
    for b in remaining_base:
        status = anchor_change(b)
        (out.fixed if status == "changed" else out.suppressed).append(b)
    return out


def anchor_change_from_diff(file_diffs: dict[str, Any]) -> Callable[[Diagnostic], str]:
    """Build the anchor classifier for :func:`diff_diagnostics` from parsed diffs."""

    def classify(d: Diagnostic) -> str:
        fd = file_diffs.get(d.file)
        if fd is None or not fd.base_line_changed(d.line):
            return "unchanged"
        old = fd.removed.get(d.line, "")
        added_texts = list(fd.added.values())
        for text in added_texts:
            if text.strip().startswith(old.strip()) and any(
                m in text[len(old.rstrip()) :] for m in SUPPRESSION_MARKERS
            ):
                return "suppression"
        return "changed"

    return classify
