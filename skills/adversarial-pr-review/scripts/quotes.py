"""Verbatim quote checks for findings and citations, with relocation by exact search."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class QuoteCheck:
    ok: bool
    line: int | None
    reason: str


def _norm(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if line.strip()]


def _read(root: Path, rel: str) -> list[str] | None:
    path = (root / rel).resolve()
    try:
        path.relative_to(root.resolve())
    except ValueError:
        return None
    if not path.is_file():
        return None
    return path.read_text(encoding="utf-8", errors="replace").splitlines()


def check(root: Path, rel: str, line: int, quote: str) -> QuoteCheck:
    """Is ``quote`` (one or more lines, compared without surrounding whitespace) in ``rel`` at
    ``line``? If not, relocate it by exact search in the same file, preferring the nearest match."""
    lines = _read(root, rel)
    if lines is None:
        return QuoteCheck(False, None, f"{rel} does not exist on this side")
    wanted = _norm(quote.splitlines())
    if not wanted:
        return QuoteCheck(False, None, "empty quote")
    stripped = [x.strip() for x in lines]

    def matches_at(start: int) -> bool:
        index, k = start, 0
        while k < len(wanted) and index < len(stripped):
            if not stripped[index]:
                index += 1
                continue
            if stripped[index] != wanted[k] and not (len(wanted) == 1 and wanted[0] in stripped[index]):
                return False
            index += 1
            k += 1
        return k == len(wanted)

    if 1 <= line <= len(lines) and matches_at(line - 1):
        return QuoteCheck(True, line, "verbatim")
    hits = [i + 1 for i in range(len(stripped)) if stripped[i] and matches_at(i)]
    if not hits:
        return QuoteCheck(False, None, f"quote not found in {rel}")
    best = min(hits, key=lambda n: abs(n - line))
    return QuoteCheck(True, best, f"relocated from line {line} to {best}")
