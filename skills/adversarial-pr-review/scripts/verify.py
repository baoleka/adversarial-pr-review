"""Verification: pick the findings to verify, batch them by file, render the verifier's brief (bare
claims only), and apply its rulings under the evidence bar, re-checking every citation."""

from __future__ import annotations

import json
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from string import Template
from typing import Any

import quotes
from briefs import nonce_for

from common import PROMPTS_DIR

VERIFY_ALL = ("critical", "major")
OUTPUT_TAIL = 3000

EXAMPLE = {
    "rulings": [
        {
            "finding_ref": "C1",
            "verdict": "CONFIRMED",
            "evidence_kind": "citation_chain",
            "citations": [
                {"side": "head", "file": "src/pkg/ranges.py", "line": 42, "quote": "if value >= self.max:"},
                {"side": "head", "file": "src/pkg/ranges.py", "line": 43, "quote": "return self.max - 1"},
            ],
            "counter_test": None,
            "expectation_source": "docs",
            "severity_assessment": "agree",
            "reason": "The docstring says both bounds are inclusive; line 42 now excludes the upper bound.",
        }
    ]
}


def selectable(finding: Mapping[str, Any]) -> bool:
    replay = finding.get("replay")
    return not (replay and replay["classification"] == "pre-existing")


def select(
    lane_findings: Sequence[dict[str, Any]], verify_cap: int, always: Collection[str] = ()
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """(to verify, left unverified): every critical and major finding and every finding in
    ``always`` (regressions of the author's fix, at any severity); other minor ones up to the cap."""
    eligible = [f for f in lane_findings if selectable(f)]
    major = [f for f in eligible if f["severity"] in VERIFY_ALL or f["id"] in always]
    minor = [f for f in eligible if f["severity"] == "minor" and f["id"] not in always]
    chosen = major + minor[:verify_cap]
    skipped = minor[verify_cap:]
    return chosen, skipped


def batches(chosen: Sequence[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for finding in sorted(chosen, key=lambda f: (f["location"]["file"], f["location"]["start_line"], f["id"])):
        out.setdefault(finding["location"]["file"], []).append(finding)
    return dict(sorted(out.items()))


def _tail(path: str | None) -> str:
    if not path or not Path(path).is_file():
        return ""
    return Path(path).read_text(encoding="utf-8", errors="replace")[-OUTPUT_TAIL:]


def claim_text(ref: str, finding: Mapping[str, Any]) -> str:
    """The bare claim: no reasoning, chain, suggested fix, lane, model or count."""
    loc = finding["location"]
    where = f"{loc['side']} `{loc['file']}` lines {loc['start_line']}-{loc['end_line']}"
    if loc["symbol"]:
        where += f", in `{loc['symbol']}`"
    scenario = finding["failure_scenario"]
    lines = [
        f"### {ref}: {finding['title']}",
        "",
        f"- Claimed severity: {finding['severity']}; category: {finding['category']}; kind: {finding['kind']}",
        f"- Location: {where}",
    ]
    if loc["quote"]:
        lines += ["- Quoted code:", "", "```", loc["quote"], "```"]
    blamed = ", ".join(f"{a['side']} `{a['file']}`:{a['line']}" for a in finding["introduced_by"]) or "none"
    lines += [
        f"- Changed lines it blames: {blamed}",
        f"- Conditions: {scenario['conditions']}",
        f"- Expected: {scenario['expected']}",
        f"- Claimed actual behavior: {scenario['actual']}",
    ]
    replay = finding.get("replay")
    repro = finding["evidence"]["repro"]
    if replay and repro:
        lines += [
            f"- Replay: **{replay['classification']}** ({replay['reason']}).",
            f"  - head: {replay['head']['outcome']}"
            + (f" ({replay['head']['signature']})" if replay["head"]["signature"] else ""),
            f"  - base: {replay['base']['outcome']}"
            + (f" ({replay['base']['signature']})" if replay["base"]["signature"] else ""),
            f"- Repro (`{repro['filename']}`, kind {repro['kind']}; expected failure: {repro['expected_failure']}; "
            f"expected difference: {repro['expected_difference']}):",
            "",
            "```python",
            repro["content"].rstrip(),
            "```",
        ]
        head_out = _tail(replay["head"].get("output_ref"))
        if head_out:
            lines += ["", "Head output (tail):", "", "```", head_out.rstrip(), "```"]
        base_out = _tail(replay["base"].get("output_ref"))
        if base_out:
            lines += ["", "Base output (tail):", "", "```", base_out.rstrip(), "```"]
    else:
        lines.append("- Replay: none (the claim came without a runnable repro).")
    return "\n".join(lines)


def render(
    *,
    run_id: str,
    lane_id: str,
    file: str,
    intent: str,
    head_root: Path,
    base_root: Path,
    scratch: Path,
    out_path: Path,
    replay_section: str,
    diff: str,
    refs: Mapping[str, Mapping[str, Any]],
) -> str:
    nonce = nonce_for(run_id, lane_id)
    template = Template((PROMPTS_DIR / "verifier.md").read_text(encoding="utf-8"))
    return template.substitute(
        file=file,
        nonce=nonce,
        intent=intent.strip() or "(no description given)",
        head_root=head_root,
        base_root=base_root,
        scratch=scratch,
        out_path=out_path,
        replay_section=replay_section,
        diff=diff.rstrip("\n") or "(no textual diff)",
        claims="\n\n".join(claim_text(ref, f) for ref, f in refs.items()),
        example=json.dumps(EXAMPLE, indent=2),
    )


def _citations_ok(cites: Sequence[Mapping[str, Any]], roots: Mapping[str, Path]) -> bool:
    return all(quotes.check(roots[c["side"]], c["file"], int(c["line"]), c["quote"]).ok for c in cites)


def _touches_change(cites: Sequence[Mapping[str, Any]], finding: Mapping[str, Any]) -> bool:
    blamed = {(a["side"], a["file"], int(a["line"])) for a in finding["introduced_by"]}
    return any((c["side"], c["file"], int(c["line"])) in blamed for c in cites)


def apply_rulings(
    refs: Mapping[str, dict[str, Any]],
    payload: Mapping[str, Any],
    roots: Mapping[str, Path],
    by: Mapping[str, str],
    verifier_replays: Sequence[str],
) -> dict[str, str]:
    """Write `verification` on each referenced finding; return notes per finding id.

    A ruling counts only with evidence the driver can check: CONFIRMED needs executed output (a
    replay classified introduced, or a repro the verifier ran) or a citation chain that re-checks;
    REFUTED needs a re-checked file:line or a counter-test the verifier ran. Otherwise it becomes
    UNSUBSTANTIATED. A confirmed chain that never touches a blamed changed line is out of scope.
    """
    notes: dict[str, str] = {}
    for ruling in payload.get("rulings", []):
        finding = refs.get(str(ruling.get("finding_ref")))
        if finding is None or finding.get("verification"):
            continue
        verdict = ruling["verdict"]
        kind = ruling["evidence_kind"]
        cites = list(ruling["citations"])
        cites_ok = _citations_ok(cites, roots)
        introduced = bool(finding.get("replay")) and finding["replay"]["classification"] == "introduced"
        executed_ref = verifier_replays[-1] if verifier_replays else None
        note = ""
        if verdict in ("CONFIRMED", "REFUTED") and not cites_ok:
            verdict, note = "UNSUBSTANTIATED", "a citation did not match the code"
        if verdict == "CONFIRMED":
            executed = kind == "executed" and (introduced or bool(verifier_replays))
            chain = kind == "citation_chain" and bool(cites)
            if not (executed or chain):
                verdict, note = "UNSUBSTANTIATED", f"CONFIRMED without checkable evidence ({kind})"
            elif chain and not executed and not introduced and not _touches_change(cites, finding):
                note = "out-of-scope: the confirmed chain does not pass through a changed line"
        elif verdict == "REFUTED":
            ok = (kind == "file_line" and bool(cites)) or (kind == "counter_test" and bool(verifier_replays))
            if not ok:
                verdict, note = "UNSUBSTANTIATED", f"REFUTED without checkable evidence ({kind})"
        evidence_kind = kind if verdict != "UNSUBSTANTIATED" else "none"
        finding["verification"] = {
            "verdict": verdict,
            "by": {"model": by["model"], "call_id": by["call_id"]},
            "evidence": {
                "kind": evidence_kind,
                "citations": cites if cites_ok else [],
                "executed_ref": executed_ref if evidence_kind in ("executed", "counter_test") else None,
            },
            "expectation_source": ruling["expectation_source"],
            "severity_assessment": ruling["severity_assessment"],
        }
        if note:
            notes[finding["id"]] = note
    return notes
