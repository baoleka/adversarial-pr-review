"""Synthesis (no LLM): pre-existing classification, computed confidence, buckets and blocking.

Confidence:
- certain: a gate or check finding, or a replay classified introduced plus CONFIRMED;
- high: CONFIRMED with a re-checked citation chain (or executed output) through a changed line;
- medium: UNSUBSTANTIATED (the number of lanes that reported it is shown, never used);
- low: REFUTED (contested when its replay was classified introduced);
- unverified: never verified.

Buckets: pre-existing findings never block; neither do fixed, deferred or suppressed ones, or late
findings (new findings on code unchanged since the previous round, without executed evidence). A
finding upheld by adjudication blocks. Critical and major findings block at certain or high
confidence and must be addressed at medium confidence, except lane findings below the evidence
bar (reasoned only, and the verifier supplied no evidence), which are listed as non-blocking.

A regression of the author's fix (a new behavior defect on lines the fix added, flagged
``fix_regression`` by the loop) blocks at any severity once verified at certain or high
confidence. Below that, a minor or nit one is non-blocking. Other minor and nit findings never block.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

import findings as fmod

MAJOR = ("critical", "major")


@dataclass
class Synthesis:
    findings: list[dict[str, Any]]
    out_of_scope: list[dict[str, Any]]
    notes: dict[str, list[str]] = field(default_factory=dict)
    unverified_major: list[str] = field(default_factory=list)


def confidence(finding: Mapping[str, Any]) -> str:
    if finding["origin"] in ("gate", "check"):
        return "certain"
    verification = finding.get("verification")
    if not verification:
        return "unverified"
    verdict = verification["verdict"]
    introduced = bool(finding.get("replay")) and finding["replay"]["classification"] == "introduced"
    if verdict == "CONFIRMED":
        return "certain" if introduced else "high"
    if verdict == "REFUTED":
        return "low"
    return "medium"


def synthesize(
    mechanical: Sequence[dict[str, Any]],
    lane: Sequence[dict[str, Any]],
    out_of_scope: Sequence[dict[str, Any]],
    below_bar: Sequence[str],
    verifier_notes: Mapping[str, str],
    known_preexisting: Sequence[Mapping[str, Any]] = (),
) -> Synthesis:
    result = Synthesis(findings=[], out_of_scope=[dict(f) for f in out_of_scope])
    below = set(below_bar)
    known = {(k["key"].get("file"), k["key"].get("symbol"), k["key"].get("rule")) for k in known_preexisting}
    for original in list(mechanical) + list(lane):
        f = dict(original)
        notes: list[str] = []
        f["confidence"] = confidence(f)
        f["blocking"] = False
        replay = f.get("replay")
        note = verifier_notes.get(f["id"], "")
        if note:
            notes.append(note)
        if note.startswith("out-of-scope"):
            f["bucket"] = "out-of-scope"
            f["preexisting"] = {"value": False, "reason": "not-anchored"}
            result.out_of_scope.append(f)
            result.notes[f["id"]] = notes
            continue
        key = (f["location"]["file"], f["location"]["symbol"], f["category"])
        if replay and replay["classification"] == "pre-existing":
            f["preexisting"] = {"value": True, "reason": "replay"}
        elif key in known:
            f["preexisting"] = {"value": True, "reason": "known_preexisting"}
        status = f["status"]
        adjudication = f.get("adjudication") or {}
        if status == "suppressed":
            f["bucket"] = "suppressed"
        elif f["preexisting"]["value"]:
            f["bucket"] = "pre-existing"
        elif status in ("fixed", "deferred"):
            f["bucket"] = "non-blocking"
        elif status == "refuted-by-adjudication":
            introduced = replay is not None and replay["classification"] == "introduced"
            f["bucket"] = "contested" if introduced else "non-blocking"
            if introduced:
                notes.append("rejected by adjudication, but its repro fails at head and passes at base")
        elif adjudication.get("ruling") == "UPHOLD":
            f["bucket"], f["blocking"] = "blocking", True
            notes.append("upheld by adjudication")
        elif f.get("late"):
            f["bucket"] = "non-blocking"
        elif f["severity"] in MAJOR or f.get("fix_regression"):
            conf = f["confidence"]
            minor_fix_regression = f["severity"] not in MAJOR
            if conf in ("certain", "high"):
                f["bucket"], f["blocking"] = "blocking", True
                if minor_fix_regression:
                    notes.append("blocks at any severity: a verified regression introduced by the author's fix")
            elif conf == "medium" and minor_fix_regression:
                f["bucket"] = "non-blocking"
                notes.append("a possible regression of the author's fix, not confirmed by verification")
            elif conf == "medium":
                if f["id"] in below:
                    f["bucket"] = "non-blocking"
                    notes.append("below the evidence bar: reasoned only, and verification supplied no evidence")
                else:
                    f["bucket"] = "must-address"
            elif conf == "low":
                if replay and replay["classification"] == "introduced":
                    f["bucket"] = "contested"
                    notes.append("refuted, but its repro fails at head and passes at base")
                else:
                    f["bucket"], f["status"] = "non-blocking", "refuted"
            else:
                f["bucket"] = "non-blocking"
                result.unverified_major.append(f["id"])
                notes.append("not verified")
        else:
            f["bucket"] = "non-blocking"
        if notes:
            result.notes[f["id"]] = notes
        result.findings.append(f)
    result.findings.sort(key=fmod.sort_key)
    result.out_of_scope.sort(key=fmod.sort_key)
    return result


def by_bucket(found: Sequence[Mapping[str, Any]], bucket: str) -> list[str]:
    return [f["id"] for f in found if f["bucket"] == bucket]
