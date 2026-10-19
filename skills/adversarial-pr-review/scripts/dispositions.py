"""Author dispositions: how the author answers each finding in loop mode.

- Blocking and must-address findings take `fix` or `dispute`; a dispute needs evidence (at least
  one `side:file:line` citation, quoted by the driver itself from that side's tree).
- Gate and check findings can't be disputed: the author fixes them, or the run escalates.
- Minor and nit findings take `fix` or `defer`.
- Nobody marks a finding refuted; only verification and adjudication do.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from common import ReviewError, read_json

ANSWER_REQUIRED = ("blocking", "must-address")
OPEN = ("open", "not-fixed", "regressed")
CITE = re.compile(r"^(head|base):(.+):(\d+)$")


def current_findings(store: Mapping[str, Any], round_no: int) -> list[dict[str, Any]]:
    return list(store["rounds"][str(round_no)].get("final", []))


def to_answer(found: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [f for f in found if f["bucket"] in ANSWER_REQUIRED and f["status"] in OPEN]


def optional(found: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    return [
        f for f in found if f["bucket"] == "non-blocking" and f["severity"] in ("minor", "nit") and f["status"] in OPEN
    ]


def resolve_cite(cite: str, roots: Mapping[str, Path]) -> dict[str, Any]:
    match = CITE.match(cite.strip())
    if not match:
        raise ReviewError(f"--cite must look like head:path/to/file.py:42, not {cite!r}")
    side, rel, line_text = match.groups()
    root = roots[side].resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ReviewError(f"{rel} does not exist at {side}")
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    line = int(line_text)
    if not 1 <= line <= len(lines) or not lines[line - 1].strip():
        raise ReviewError(f"{rel}:{line} at {side} is not a non-empty line")
    return {"side": side, "file": rel, "line": line, "quote": lines[line - 1].strip()[:2000]}


def record(
    found: Sequence[dict[str, Any]],
    pending: dict[str, Any],
    finding_id: str,
    action: str,
    note: str,
    diverges: bool,
    reason: str | None,
    cites: Sequence[str],
    roots: Mapping[str, Path],
) -> dict[str, Any]:
    """Validate one disposition and store it in ``pending`` (keyed by finding id)."""
    finding = next((f for f in found if f["id"] == finding_id), None)
    if finding is None:
        raise ReviewError(f"{finding_id} is not a finding of this round")
    if action not in ("fix", "dispute", "defer"):
        raise ReviewError("the action is fix, dispute or defer (only verification can refute a finding)")
    must = finding in to_answer(found)
    if not must and finding not in optional(found):
        raise ReviewError(f"{finding_id} needs no answer ({finding['bucket']}, {finding['status']})")
    if action == "dispute":
        if finding["origin"] != "lane":
            raise ReviewError(f"{finding_id} is a {finding['origin']} finding: fix it (it can't be disputed)")
        if not must:
            raise ReviewError(f"{finding_id} is minor or a nit: fix or defer it")
        if not cites:
            raise ReviewError("a dispute needs evidence: at least one --cite head:path:line")
    if action == "defer" and must:
        raise ReviewError(f"{finding_id} is {finding['bucket']}: fix or dispute it")
    if not note.strip():
        raise ReviewError("--note is required: say what you changed, or why the finding is wrong")
    if diverges and not (reason or "").strip():
        raise ReviewError("--diverges needs --reason: say why your fix differs from the suggested one")
    disposition = {
        "action": action,
        "note": note.strip()[:2000],
        "diverges_from_suggestion": bool(diverges),
        "reason": (reason or "").strip()[:2000] or None,
        "evidence": [resolve_cite(c, roots) for c in cites],
    }
    pending[finding_id] = disposition
    return disposition


def missing(found: Sequence[Mapping[str, Any]], pending: Mapping[str, Any]) -> list[str]:
    return sorted(str(f["id"]) for f in to_answer(found) if f["id"] not in pending)


def ingest(ctx: Any, path: str | None) -> None:
    """Close the author step: every blocking and must-address finding needs an answer."""
    store = read_json(ctx.run.dir / "findings.json")
    found = current_findings(store, ctx.round)
    pending = ctx.state["rounds"][str(ctx.round)].setdefault("dispositions", {})
    gaps = missing(found, pending)
    if gaps:
        raise ReviewError("answer every blocking and must-address finding first (fix or dispute): " + ", ".join(gaps))
    ctx.run.event("author-done", round=ctx.round, dispositions=len(pending))
    ctx.state["round"] = ctx.round + 1
    ctx.state["phase"] = "SNAPSHOT"
    ctx.state["rounds"].setdefault(str(ctx.state["round"]), {})
