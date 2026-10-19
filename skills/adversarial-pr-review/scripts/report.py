"""Render report.md and the author's compact list, deterministically (no clock, sorted inputs)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

SECTIONS = (
    ("blocking", "Blocking"),
    ("must-address", "Must address"),
    ("contested", "Contested"),
    ("non-blocking", "Non-blocking"),
)
GUARD_NOTE = (
    "Ignored files in the author's tree (virtualenvs, caches) are protected only by the guard hooks; "
    "tracked and untracked files are also covered by the before/after tree hash."
)


def _loc(f: Mapping[str, Any]) -> str:
    loc = f["location"]
    span = f"{loc['start_line']}" if loc["start_line"] == loc["end_line"] else f"{loc['start_line']}-{loc['end_line']}"
    side = "" if loc["side"] == "head" else " (base)"
    return f"`{loc['file']}:{span}`{side}"


def _origin(f: Mapping[str, Any]) -> str:
    if f["origin"] != "lane":
        return str(f["origin"])
    lenses = sorted({str(p.get("lens") or p.get("lane")) for p in f["provenance"]})
    return "lane: " + ", ".join(lenses)


def _evidence(f: Mapping[str, Any]) -> list[str]:
    lines = []
    replay = f.get("replay")
    repro = f["evidence"]["repro"]
    if replay and repro:
        head, base = replay["head"], replay["base"]
        lines.append(
            f"- Replay of `{repro['filename']}`: **{replay['classification']}** ({replay['reason']}); "
            f"head {head['outcome']}{' — ' + head['signature'] if head['signature'] else ''}; "
            f"base {base['outcome']}{' — ' + base['signature'] if base['signature'] else ''}"
        )
    elif f["origin"] == "lane":
        lines.append(f"- Evidence: {f['evidence']['level']}")
    if f["evidence"]["observed_output"] and f["origin"] != "lane":
        out = f["evidence"]["observed_output"].strip().splitlines()
        lines.append("- Output: `" + (out[0][:200] if out else "") + "`" + (" …" if len(out) > 1 else ""))
    verification = f.get("verification")
    if verification:
        cites = verification["evidence"]["citations"]
        cited = "; ".join(f"`{c['file']}:{c['line']}`" for c in cites[:4])
        lines.append(
            f"- Verification: {verification['verdict']} ({verification['evidence']['kind']}"
            f"{', ' + cited if cited else ''}; expectation from {verification['expectation_source']}; "
            f"severity {verification['severity_assessment']})"
        )
    return lines


def finding_block(f: Mapping[str, Any], notes: Sequence[str]) -> str:
    scenario = f["failure_scenario"]
    lines = [
        f"### [{f['severity']}] {f['title']} — {_loc(f)}",
        "",
        f"- ID `{f['id']}`, {f['category']}, confidence **{f['confidence']}**, {_origin(f)}, status {f['status']}",
        f"- When: {scenario['conditions']}",
        f"- Expected: {scenario['expected']}",
        f"- Actual: {scenario['actual']}",
        *_evidence(f),
    ]
    if f["suggested_fix"]:
        lines.append(f"- Suggested fix: {f['suggested_fix']}")
    if f.get("disposition"):
        d = f["disposition"]
        lines.append(f"- Author: {d['action']} — {d['note']}")
    for note in notes:
        lines.append(f"- Note: {note}")
    return "\n".join(lines)


def _gate_table(summary: Mapping[str, Any]) -> list[str]:
    if not summary:
        return ["No gates are configured."]
    rows = [
        "| Gate | Base | Head | New | Changed | Fixed | Suppressed | Persisting | Error count |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for name in sorted(summary):
        g = summary[name]
        count = g.get("error_count")
        counted = f"{count['base']} → {count['head']} (limit {count['limit']})" if count else ""
        rows.append(
            f"| {name} | {g.get('base')} | {g.get('head')} | {g.get('new', '')} | {g.get('changed', '')} | "
            f"{g.get('fixed', '')} | {g.get('suppressed', '')} | {g.get('persisting', '')} | {counted} |"
        )
    return rows


def _lane_table(lanes: Sequence[Mapping[str, Any]]) -> list[str]:
    if not lanes:
        return ["No review lanes ran (the change has no reviewable lines)."]
    rows = [
        "| Call | Model | Effort | Status | Raw | Kept | Rejected | Cost | Tokens in/out |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for lane in lanes:
        tokens = lane.get("tokens") or {}
        rows.append(
            f"| {lane['id']} | {lane['model']} | {lane['effort']} | {lane['status']} | {lane.get('findings_raw', '')} | "
            f"{lane.get('kept', '')} | {lane.get('rejected', '')} | ${lane.get('cost_usd', 0):.2f} | "
            f"{tokens.get('input', 0) + tokens.get('cache_read', 0) + tokens.get('cache_write', 0)}/{tokens.get('output', 0)} |"
        )
    return rows


def render(
    verdict: Mapping[str, Any], store_round: Mapping[str, Any], notes: Mapping[str, Sequence[str]], intent: str
) -> str:
    found = store_round["final"]
    out = [f"# Review report — {verdict['run_id']}, round {verdict['round']}", ""]
    out += ["## Intent", "", intent.strip() or "(none given)", ""]
    out += ["## Verdict", "", f"**{verdict['verdict']}** — {verdict['reason']}", ""]
    for e in verdict["escalations"]:
        out.append(
            f"- **{e['condition']}**: {e['detail']}"
            + (f" Decision needed: {e['decision_needed']}" if e.get("decision_needed") else "")
        )
    if verdict["escalations"]:
        out.append("")
    for bucket, title in SECTIONS:
        items = [f for f in found if f["bucket"] == bucket]
        out += [f"## {title} ({len(items)})", ""]
        out += [finding_block(f, notes.get(f["id"], [])) + "\n" for f in items] or ["None.", ""]
    pre = [f for f in found if f["bucket"] == "pre-existing"]
    persisting = store_round.get("appendix", {}).get("persisting", [])
    out += [f"## Pre-existing ({len(pre)} findings, {len(persisting)} persisting diagnostics)", ""]
    if pre or persisting:
        out += ["<details><summary>Show</summary>", ""]
        out += [f"- [{f['severity']}] {f['title']} — {_loc(f)} ({f['preexisting']['reason']})" for f in pre]
        out += [f"- {d['tool']} {d['rule']} `{d['file']}:{d['line']}` {d['message'][:120]}" for d in persisting[:200]]
        out += ["", "</details>", ""]
    else:
        out += ["None.", ""]
    oos = store_round.get("out_of_scope_final", [])
    out += [f"## Out of scope ({len(oos)})", ""]
    out += [f"- [{f['severity']}] {f['title']} — {_loc(f)}" for f in oos] or ["None."]
    out.append("")
    rejected = store_round.get("rejected", [])
    out += [f"## Rejected lane findings ({len(rejected)})", ""]
    by_lane: dict[str, list[str]] = {}
    for r in rejected:
        by_lane.setdefault(str(r.get("lane")), []).append(str(r.get("reason")))
    out += [
        f"- {lane}: {len(reasons)} — " + "; ".join(sorted(set(reasons)))[:400]
        for lane, reasons in sorted(by_lane.items())
    ] or ["None."]
    out.append("")
    out += ["## Gates", "", *_gate_table(verdict["gate_summary"]), ""]
    out += ["## Lanes and calls", "", *_lane_table(verdict["lanes"]), ""]
    cov = verdict["coverage"]
    out += [
        "## Review coverage",
        "",
        f"{cov['reviewed_lines']} of {cov['changed_lines']} reviewable changed lines reviewed ({cov['ratio']:.0%}); "
        f"{cov['excluded_lines']} lines excluded (generated, vendored, lockfiles).",
        "",
    ]
    cost = verdict["cost"]
    out += ["## Cost", "", f"${cost['total_usd']:.2f} of a ${cost['budget_usd']:.2f} budget."]
    out += [f"- {kind}: ${value:.2f}" for kind, value in sorted(cost["by_kind"].items())]
    out.append("")
    meta = [
        f"- Base ref `{verdict['base_ref']}`, merge-base `{verdict['merge_base']}`",
        f"- Snapshot tree `{verdict['snapshot_tree']}`, commit `{verdict['snapshot_commit']}`",
        f"- Mode {verdict['mode']}; single model family: {str(verdict['single_family']).lower()}",
        f"- Replay isolation: {verdict['replay_isolation']}",
        f"- Integrity: {'ok' if verdict['integrity']['ok'] else 'violations: ' + '; '.join(v['detail'] for v in verdict['integrity']['violations'])}",
        f"- {GUARD_NOTE}",
    ]
    if verdict.get("stale_base"):
        meta.append(f"- Stale base: {verdict['stale_base']}")
    out += ["## Run", "", *meta, ""]
    return "\n".join(out)


def author_list(found: Sequence[Mapping[str, Any]]) -> str:
    must = [
        f
        for f in found
        if f["bucket"] in ("blocking", "must-address") and f["status"] in ("open", "not-fixed", "regressed")
    ]
    minor = [
        f
        for f in found
        if f["bucket"] == "non-blocking" and f["severity"] in ("minor", "nit") and f["status"] == "open"
    ]
    lines = ["# Findings to address", ""]
    for f in must:
        replay = f.get("replay") or {}
        repro = f["evidence"]["repro"]
        lines += [
            f"## {f['id']} [{f['severity']}, {f['bucket']}] {f['title']}",
            f"- Where: {_loc(f)}",
            f"- When {f['failure_scenario']['conditions']}: expected {f['failure_scenario']['expected']}; actual {f['failure_scenario']['actual']}",
        ]
        if repro:
            head = replay.get("head") or {}
            lines.append(
                f"- Repro `{repro['filename']}` ({repro['kind']}): head {head.get('outcome', 'not run')} {head.get('signature') or ''}".rstrip()
            )
        if f["suggested_fix"]:
            lines.append(f"- Suggested fix: {f['suggested_fix']}")
        lines.append("")
    if minor:
        lines += ["## Minor and nit (fix or defer)", ""]
        lines += [f"- {f['id']} [{f['severity']}] {f['title']} — {_loc(f)}" for f in minor]
    return "\n".join(lines) + "\n"
