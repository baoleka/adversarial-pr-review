"""Normalize lane output: validate, coerce, check quotes, anchor in the diff, fingerprint, dedupe.

No LLM runs here. A finding that can't be anchored in the diff goes to the out-of-scope appendix;
an invalid one is rejected and counted against its lane.
"""

from __future__ import annotations

import difflib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import findings as fmod
import quotes
from diffs import FileDiff

from common import SCHEMAS_DIR, read_json
from jsonschema_lite import validate

MAX_NITS_PER_RUN = 3
SIMILARITY = 0.8


@dataclass
class LaneMeta:
    lane: str
    model: str
    lens: str
    unit: str | None
    raw_ref: str


@dataclass
class Normalized:
    kept: list[dict[str, Any]] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    out_of_scope: list[dict[str, Any]] = field(default_factory=list)
    below_evidence_bar: list[str] = field(default_factory=list)


def wire_finding_schema() -> dict[str, Any]:
    wire = read_json(SCHEMAS_DIR / "finding.wire.schema.json")
    assert isinstance(wire, dict)
    return {**wire["$defs"]["finding"], "$defs": wire["$defs"]}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 1 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number >= 1 else None
    return None


def coerce(raw: Any) -> Any:
    """Light, safe coercion before validation: numeric strings to ints, strip strings."""
    if not isinstance(raw, dict):
        return raw
    out = dict(raw)
    loc = out.get("location")
    if isinstance(loc, dict):
        loc = dict(loc)
        for key in ("start_line", "end_line"):
            number = _int(loc.get(key))
            if number is not None:
                loc[key] = number
        if isinstance(loc.get("file"), str):
            loc["file"] = loc["file"].strip().removeprefix("./")
        out["location"] = loc
    anchors = out.get("introduced_by")
    if isinstance(anchors, list):
        fixed = []
        for anchor in anchors:
            if isinstance(anchor, dict):
                anchor = dict(anchor)
                number = _int(anchor.get("line"))
                if number is not None:
                    anchor["line"] = number
                if isinstance(anchor.get("file"), str):
                    anchor["file"] = anchor["file"].strip().removeprefix("./")
            fixed.append(anchor)
        out["introduced_by"] = fixed
    return out


def in_diff(anchor: Mapping[str, Any], file_diffs: Mapping[str, FileDiff]) -> bool:
    fd = file_diffs.get(str(anchor.get("file")))
    if fd is None:
        return False
    line = int(anchor.get("line", 0))
    return line in (fd.added if anchor.get("side", "head") == "head" else fd.removed)


def normalize_lane(
    payload: Any,
    meta: LaneMeta,
    roots: Mapping[str, Path],
    file_diffs: Mapping[str, FileDiff],
    round_no: int,
) -> Normalized:
    """``roots`` maps side ("head"/"base") to its worktree."""
    out = Normalized()
    schema = wire_finding_schema()
    raw_findings = payload.get("findings") if isinstance(payload, dict) else None
    if not isinstance(raw_findings, list):
        out.rejected.append({"lane": meta.lane, "reason": "output has no findings list"})
        return out
    for index, raw in enumerate(raw_findings):
        item = coerce(raw)
        problems = validate(item, schema)
        if problems:
            out.rejected.append({"lane": meta.lane, "index": index, "reason": "schema: " + "; ".join(problems[:5])})
            continue
        loc = item["location"]
        side = loc["side"]
        root = roots[side]
        start = int(loc["start_line"])
        end = max(start, int(loc["end_line"]))
        quote = loc["quote"]
        if item["kind"] == "defect":
            if not quote:
                out.rejected.append({"lane": meta.lane, "index": index, "reason": "a defect needs a quote"})
                continue
            result = quotes.check(root, loc["file"], start, quote)
            if not result.ok:
                out.rejected.append({"lane": meta.lane, "index": index, "reason": f"quote: {result.reason}"})
                continue
            if result.line != start:
                end = result.line + (end - start) if result.line else end
                start = result.line or start
        else:
            fd = file_diffs.get(loc["file"])
            if fd is None:
                out.rejected.append(
                    {"lane": meta.lane, "index": index, "reason": "an omission must be in a changed file"}
                )
                continue
            start = fd.nearest_hunk_line(start)
            end = start
            side = "head"
        chain = []
        chain_ok = True
        for cite in item["evidence"]["citation_chain"]:
            check = quotes.check(roots[cite["side"]], cite["file"], int(cite["line"]), cite["quote"])
            if not check.ok:
                chain_ok = False
                break
            chain.append({**cite, "line": check.line or cite["line"]})
        level = item["evidence"]["level"]
        if level == "traced" and (not chain_ok or not chain):
            level = "reasoned"
        anchors = [a for a in item["introduced_by"] if in_diff(a, file_diffs)]
        if item["kind"] == "defect" and not anchors and loc["file"] in file_diffs:
            # The lane left introduced_by empty, but the quoted lines are themselves changed lines.
            fd = file_diffs[loc["file"]]
            changed = fd.added if side == "head" else fd.removed
            anchors = [{"side": side, "file": loc["file"], "line": n} for n in range(start, end + 1) if n in changed][
                :3
            ]
        if item["kind"] == "omission" and not anchors:
            anchors = (
                [{"side": "head", "file": loc["file"], "line": start}] if start in file_diffs[loc["file"]].added else []
            )
        finding = fmod.make(
            origin="lane",
            category=item["category"],
            severity=item["severity"],
            title=item["title"].strip() or "Untitled finding",
            file=loc["file"],
            start_line=start,
            end_line=end,
            side=side,
            quote=quote,
            symbol=loc["symbol"],
            anchor_text=quote if quote else f"omission {item['category']} {item['title']}",
            introduced_by=anchors,
            conditions=item["failure_scenario"]["conditions"],
            expected=item["failure_scenario"]["expected"],
            actual=item["failure_scenario"]["actual"],
            evidence_level=level,
            observed_output=item["evidence"]["observed_output"],
            suggested_fix=item["suggested_fix"],
            kind=item["kind"],
            round_no=round_no,
            raw_ref=f"{meta.raw_ref}#{index}",
            lane=meta.lane,
            model=meta.model,
            lens=meta.lens,
            citation_chain=chain if chain_ok else [],
            repro=item["evidence"]["repro"],
            self_confidence=item["self_confidence"],
        )
        if not anchors:
            finding["bucket"] = "out-of-scope"
            finding["preexisting"] = {"value": False, "reason": "not-anchored"}
            out.out_of_scope.append(finding)
            continue
        if finding["severity"] in ("critical", "major") and level == "reasoned" and not finding["evidence"]["repro"]:
            out.below_evidence_bar.append(finding["id"])
        out.kept.append(finding)
    return out


def similar(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    la, lb = a["location"], b["location"]
    if la["file"] != lb["file"] or la["side"] != lb["side"]:
        return False
    if a["fingerprint"] == b["fingerprint"]:
        return True
    if la["symbol"] and la["symbol"] == lb["symbol"] and la["quote"] and lb["quote"]:
        qa, qb = quote_lines(la["quote"]), quote_lines(lb["quote"])
        # One lane quoting more of the same lines than another is the same anchor.
        if contains(qa, qb) or contains(qb, qa):
            return True
        return difflib.SequenceMatcher(None, "\n".join(qa), "\n".join(qb)).ratio() >= SIMILARITY
    return False


def quote_lines(quote: str) -> list[str]:
    return [fmod.normalize_anchor(line) for line in quote.splitlines() if line.strip()]


def contains(outer: list[str], inner: list[str]) -> bool:
    if not inner or len(inner) > len(outer):
        return False
    return any(outer[i : i + len(inner)] == inner for i in range(len(outer) - len(inner) + 1))


def merge_into(target: dict[str, Any], other: Mapping[str, Any]) -> None:
    target["provenance"].extend(p for p in other["provenance"] if p not in target["provenance"])
    if fmod.SEVERITY_RANK[other["severity"]] < fmod.SEVERITY_RANK[target["severity"]]:
        target["severity"] = other["severity"]
    rank = {"executed": 0, "traced": 1, "reasoned": 2}
    if rank[other["evidence"]["level"]] < rank[target["evidence"]["level"]]:
        target["evidence"] = dict(other["evidence"])
    for anchor in other["introduced_by"]:
        if anchor not in target["introduced_by"]:
            target["introduced_by"].append(anchor)


def dedupe(found: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for finding in sorted(found, key=fmod.sort_key):
        twin = next((m for m in merged if similar(m, finding)), None)
        if twin is None:
            merged.append(dict(finding))
        else:
            merge_into(twin, finding)
    nits = [f for f in merged if f["severity"] == "nit"]
    for extra in nits[MAX_NITS_PER_RUN:]:
        merged.remove(extra)
    return sorted(merged, key=fmod.sort_key)
