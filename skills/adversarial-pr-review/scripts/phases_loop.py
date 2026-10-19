"""Round N+1 of the fix loop: replay each fixed finding's repro (it must now pass), verify the fixes
in a fresh context, adjudicate disputes, and carry the previous round's findings forward under the
late-finding and suppression rules."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from string import Template
from typing import Any

import briefs
import diffs
import findings as fmod
import lanes
import normalize
import quotes
import telemetry
import verify
from engine import Ctx, Step, phase
from lanes import LaneSpec
from phases_lanes import head_commit, pool_step, rnd

from common import PROMPTS_DIR, SCRIPTS_DIR, read_json, write_json

FIXCHECK_EXAMPLE = {
    "rulings": [
        {
            "finding_ref": "C1",
            "verdict": "FIXED",
            "citations": [{"side": "head", "file": "src/pkg/ranges.py", "line": 42, "quote": "if value > self.max:"}],
            "reason": "The bound is inclusive again and the repro passes.",
        }
    ]
}
ADJUDICATOR_EXAMPLE = {
    "ruling": "UPHOLD",
    "evidence_kind": "executed",
    "citations": [],
    "reason": "The repro fails at head as claimed and passes at base; the dispute cites a guard that does not run on this path.",
}


def prev_round(ctx: Ctx) -> dict[str, Any]:
    data: dict[str, Any] = ctx.state["rounds"][str(ctx.round - 1)]
    return data


def prev_final(ctx: Ctx) -> list[dict[str, Any]]:
    store = read_json(ctx.run.dir / "findings.json")
    return list(store["rounds"][str(ctx.round - 1)].get("final", []))


def dispositions(ctx: Ctx) -> dict[str, Any]:
    data: dict[str, Any] = prev_round(ctx).get("dispositions", {})
    return data


def delta_diff(ctx: Ctx, files: Sequence[str] | None = None) -> str:
    previous = prev_round(ctx)["snapshot"]["commit"]
    return diffs.function_context_diff(ctx.repo, previous, head_commit(ctx), list(files) if files else None)


def delta_lines(ctx: Ctx) -> dict[str, diffs.FileDiff]:
    return diffs.diff_between(ctx.repo, prev_round(ctx)["snapshot"]["commit"], head_commit(ctx))


def replay_section(ctx: Ctx, lane_id: str) -> str:
    if ctx.state.get("replay_isolation") != "docker":
        return "## Running repros\n\nRepro replay is unavailable on this machine; rule from the code and the citations."
    return Template((PROMPTS_DIR / "replay-section.md").read_text(encoding="utf-8")).substitute(
        replay_cmd=lanes.replay_command(ctx.run, lane_id), replay_timeout=int(ctx.config()["replay"]["timeout_s"])
    )


# ---------------------------------------------------------------- FIXREPLAY


@phase("FIXREPLAY")
def fixreplay_phase(ctx: Ctx) -> Step:
    ps = ctx.phase_state()
    r = rnd(ctx)
    answers = dispositions(ctx)
    fixed = [f for f in prev_final(ctx) if answers.get(f["id"], {}).get("action") == "fix" and f["evidence"]["repro"]]
    if not fixed:
        r["fix_replay"] = {}
        return Step.advance()
    if "job" not in ps:
        work = ctx.run.round_dir() / "fix-replay"
        work.mkdir(parents=True, exist_ok=True)
        write_json(work / "findings.json", fixed)
        ps["out"] = str(work / "results.json")
        cfg = ctx.config()
        argv = [sys.executable, "-E", "-s", str(SCRIPTS_DIR / "replay.py"), "batch", "--run", str(ctx.run.dir)]
        argv += ["--round", str(ctx.round), "--out", ps["out"], "--findings", str(work / "findings.json")]
        timeout = min(3600, len(fixed) * 4 * int(cfg["replay"]["timeout_s"]) + 120)
        ps["job"] = ctx.run.spawn_job(
            "fixreplay", argv, ctx.run.dir, dict(os.environ), timeout, meta={"kind": "fixreplay"}
        )
        return Step.wait([ps["job"]])
    if ctx.run.poll_job(ps["job"]) == "running":
        return Step.wait([ps["job"]])
    out = Path(ps["out"])
    results: dict[str, Any] = read_json(out) if out.is_file() else {}
    # An error or a timeout is not a pass; a missing result is not a pass either.
    r["fix_replay"] = {
        f["id"]: {
            "head": results.get(f["id"], {}).get("head", {}).get("outcome", "not-run"),
            "replay": results.get(f["id"]),
        }
        for f in fixed
    }
    return Step.advance()


# ---------------------------------------------------------------- FIXCHECK


def mechanical_fix_status(prior: Mapping[str, Any], current_mechanical: Sequence[Mapping[str, Any]]) -> str:
    return "not-fixed" if any(m["fingerprint"] == prior["fingerprint"] for m in current_mechanical) else "fixed"


def fix_claim(ref: str, finding: Mapping[str, Any], fix: Mapping[str, Any], answer: Mapping[str, Any]) -> str:
    text = verify.claim_text(ref, finding)
    lines = [text, f"- The author's fix note: {answer.get('note', '')}"]
    if fix:
        lines.append(f"- Repro re-run at the new head: **{fix['head']}**")
    return "\n".join(lines)


@phase("FIXCHECK")
def fixcheck_phase(ctx: Ctx) -> Step:
    cfg = ctx.config()
    r = rnd(ctx)
    ps = ctx.phase_state()
    answers = dispositions(ctx)
    store = read_json(ctx.run.dir / "findings.json")
    mechanical_now = store["rounds"][str(ctx.round)]["mechanical"]
    prior = {f["id"]: f for f in prev_final(ctx)}
    fixes: dict[str, Any] = r.setdefault("fixes", {})
    for fid, answer in answers.items():
        if answer["action"] == "fix" and fid in prior and prior[fid]["origin"] != "lane":
            fixes[fid] = {"status": mechanical_fix_status(prior[fid], mechanical_now), "by": "gate rerun"}
    lane_fixes = [
        prior[fid]
        for fid, a in answers.items()
        if a["action"] == "fix" and fid in prior and prior[fid]["origin"] == "lane"
    ]
    if not lane_fixes:
        return Step.advance()
    if "entries" not in ps:
        lane_id = f"r{ctx.round}-fixcheck-1"
        refs = {f"C{i}": f for i, f in enumerate(lane_fixes, start=1)}
        fix_replay = r.get("fix_replay", {})
        claims = "\n\n".join(
            fix_claim(ref, f, fix_replay.get(f["id"], {}), answers[f["id"]]) for ref, f in refs.items()
        )
        brief = Template((PROMPTS_DIR / "fixcheck.md").read_text(encoding="utf-8")).substitute(
            head_root=ctx.run.wt_head,
            base_root=ctx.run.wt_base,
            scratch=lanes.scratch_dir(ctx.run, lane_id),
            out_path=lanes.out_path(ctx.run, lane_id),
            replay_section=replay_section(ctx, lane_id),
            nonce=briefs.nonce_for(ctx.state["run_id"], lane_id),
            delta=briefs.capped_diff(delta_diff(ctx))[0].rstrip("\n") or "(no code changed)",
            claims=claims,
            example=json.dumps(FIXCHECK_EXAMPLE, indent=2),
        )
        _, system = briefs.agent_system_text("review-fixcheck")
        lane_cfg = cfg["lanes"]["fixcheck"]
        spec = LaneSpec(
            id=lane_id, kind="fixcheck", agent="review-fixcheck", model=lane_cfg["model"], effort=lane_cfg["effort"],
            max_turns=int(lane_cfg["max_turns"]), timeout_s=int(lane_cfg["timeout_s"]), lens="fixcheck", unit=None,
            output_kind="fixcheck", round=ctx.round,
            estimate_usd=telemetry.estimate_call(cfg, "fixcheck", lane_cfg["model"], lane_cfg["effort"], telemetry.tokens_of(brief)),
        )  # fmt: skip
        hashes = lanes.prepare(ctx.run, spec, brief, system)
        ps["entries"] = {
            lane_id: {
                "spec": spec.to_json(),
                "status": "pending",
                "job": None,
                "hashes": hashes,
                "refs": {k: f["id"] for k, f in refs.items()},
            }
        }
        if not telemetry.can_afford(ctx.state, cfg, spec.estimate_usd):
            ctx.escalate(
                "budget",
                "the budget can't cover verifying the author's fixes",
                finding_ids=[f["id"] for f in lane_fixes],
            )
            return Step.advance()
    entries: dict[str, Any] = ps["entries"]
    running = pool_step(ctx, cfg, entries, "fixcheck", reserve=0.0)
    if running:
        return Step.wait(running, any_done=True)
    ok = [e for e in entries.values() if e["status"] == "ok"]
    if not ok:
        ctx.escalate("degraded-lane", "the fix verification call failed", finding_ids=[f["id"] for f in lane_fixes])
        return Step.advance()
    entry = ok[-1]
    spec = LaneSpec.from_json(entry["spec"])
    payload, _ = lanes.load_output(ctx.run, spec)
    rulings = {str(x["finding_ref"]): x for x in (payload or {}).get("rulings", [])}
    roots = {"head": ctx.run.wt_head, "base": ctx.run.wt_base}
    fix_replay = r.get("fix_replay", {})
    for ref, fid in entry["refs"].items():
        ruling = rulings.get(ref)
        replayed = fix_replay.get(fid)
        if replayed and replayed["head"] != "pass":
            fixes[fid] = {
                "status": "not-fixed",
                "by": "replay",
                "reason": f"the repro still ends in {replayed['head']} at head",
            }
            continue
        if ruling is None:
            fixes[fid] = {"status": "not-fixed", "by": spec.id, "reason": "no ruling"}
            continue
        cites_ok = all(
            quotes.check(roots[c["side"]], c["file"], int(c["line"]), c["quote"]).ok for c in ruling["citations"]
        )
        status = {"FIXED": "fixed", "NOT_FIXED": "not-fixed", "REGRESSED": "regressed"}[ruling["verdict"]]
        if status == "fixed" and not (cites_ok and (ruling["citations"] or replayed)):
            status = "not-fixed"
        fixes[fid] = {"status": status, "by": spec.id, "reason": str(ruling["reason"])[:500]}
    return Step.advance()


# ---------------------------------------------------------------- ADJUDICATE


def dispute_text(answer: Mapping[str, Any]) -> str:
    lines = [answer.get("note", "")]
    for cite in answer.get("evidence", []):
        lines.append(f"- cites {cite['side']} `{cite['file']}`:{cite['line']}: `{cite['quote']}`")
    return "\n".join(lines)


@phase("ADJUDICATE")
def adjudicate_phase(ctx: Ctx) -> Step:
    cfg = ctx.config()
    r = rnd(ctx)
    ps = ctx.phase_state()
    answers = dispositions(ctx)
    prior = {f["id"]: f for f in prev_final(ctx)}
    disputed = [prior[fid] for fid, a in sorted(answers.items()) if a["action"] == "dispute" and fid in prior]
    if not disputed:
        r["adjudications"] = {}
        return Step.advance()
    if "entries" not in ps:
        entries: dict[str, Any] = {}
        lane_cfg = cfg["lanes"]["adjudicator"]
        for index, finding in enumerate(disputed, start=1):
            lane_id = f"r{ctx.round}-adjudicator-{index}"
            brief = Template((PROMPTS_DIR / "adjudicator.md").read_text(encoding="utf-8")).substitute(
                head_root=ctx.run.wt_head,
                base_root=ctx.run.wt_base,
                scratch=lanes.scratch_dir(ctx.run, lane_id),
                out_path=lanes.out_path(ctx.run, lane_id),
                replay_section=replay_section(ctx, lane_id),
                nonce=briefs.nonce_for(ctx.state["run_id"], lane_id),
                claim=verify.claim_text("C1", finding),
                dispute=dispute_text(answers[finding["id"]]),
                example=json.dumps(ADJUDICATOR_EXAMPLE, indent=2),
            )
            _, system = briefs.agent_system_text("review-adjudicator")
            spec = LaneSpec(
                id=lane_id, kind="adjudicator", agent="review-adjudicator", model=lane_cfg["model"],
                effort=lane_cfg["effort"], max_turns=int(lane_cfg["max_turns"]), timeout_s=int(lane_cfg["timeout_s"]),
                lens="adjudicator", unit=None, output_kind="adjudicator", round=ctx.round,
                estimate_usd=telemetry.estimate_call(cfg, "adjudicator", lane_cfg["model"], lane_cfg["effort"], telemetry.tokens_of(brief)),
            )  # fmt: skip
            hashes = lanes.prepare(ctx.run, spec, brief, system)
            entries[lane_id] = {
                "spec": spec.to_json(),
                "status": "pending",
                "job": None,
                "hashes": hashes,
                "finding": finding["id"],
            }
        need = sum(e["spec"]["estimate_usd"] for e in entries.values())
        ps["entries"] = entries
        if need > telemetry.remaining(ctx.state, cfg):
            ctx.escalate(
                "budget",
                f"adjudicating {len(entries)} dispute(s) needs about ${need:.2f}",
                finding_ids=[f["id"] for f in disputed],
            )
            return Step.advance()
    entries = ps["entries"]
    running = pool_step(ctx, cfg, entries, "adjudicate", reserve=0.0)
    if running:
        return Step.wait(running, any_done=True)
    roots = {"head": ctx.run.wt_head, "base": ctx.run.wt_base}
    rulings: dict[str, Any] = {}
    for entry in entries.values():
        fid = entry["finding"]
        if entry["status"] != "ok":
            rulings.setdefault(
                fid,
                {
                    "ruling": "UNRESOLVED",
                    "by": entry["spec"]["id"],
                    "evidence": [],
                    "reason": "the adjudication call failed",
                },
            )
            continue
        spec = LaneSpec.from_json(entry["spec"])
        payload, _ = lanes.load_output(ctx.run, spec)
        if payload is None:
            continue
        cites = list(payload["citations"])
        cites_ok = all(quotes.check(roots[c["side"]], c["file"], int(c["line"]), c["quote"]).ok for c in cites)
        ruling = payload["ruling"]
        executed = payload["evidence_kind"] == "executed"
        if ruling == "UPHOLD" and not ((executed or payload["evidence_kind"] == "citation_chain") and cites_ok):
            ruling = "UNRESOLVED"
        if ruling == "REJECT" and not ((executed or (payload["evidence_kind"] == "file_line" and cites)) and cites_ok):
            ruling = "UNRESOLVED"
        rulings[fid] = {
            "ruling": ruling,
            "by": spec.id,
            "evidence": cites if cites_ok else [],
            "reason": str(payload["reason"])[:1000],
        }
    r["adjudications"] = rulings
    unresolved = sorted(fid for fid, x in rulings.items() if x["ruling"] == "UNRESOLVED")
    if unresolved:
        ctx.escalate(
            "adjudication-unresolved",
            "the evidence doesn't settle the author's dispute",
            finding_ids=unresolved,
            decision="Read the finding and the dispute and decide which side is right.",
        )
    return Step.advance()


# ---------------------------------------------------------------- carry-over (used by SYNTHESIZE)


def carry_over(
    prior: Sequence[Mapping[str, Any]],
    answers: Mapping[str, Any],
    fixes: Mapping[str, Any],
    adjudications: Mapping[str, Any],
    round_no: int,
) -> list[dict[str, Any]]:
    """The previous round's lane findings with their new status (gate and check findings are
    recomputed by this round's gates, so they are not carried)."""
    out = []
    for original in prior:
        if original["origin"] != "lane" or original["bucket"] in ("out-of-scope", "rejected", "suppressed"):
            continue
        f = json.loads(json.dumps(original))
        answer = answers.get(f["id"])
        if answer:
            f["disposition"] = answer
            if answer["action"] == "fix":
                f["status"] = fixes.get(f["id"], {}).get("status", "not-fixed")
                if f["status"] != "fixed" and f["severity"] in ("minor", "nit") and not f.get("fix_regression"):
                    # An unverified fix of a minor or nit finding doesn't hold the change back for
                    # another round: it is recorded as deferred, and the report says so. A regression
                    # of an earlier fix stays binding at any severity.
                    f["status"] = "deferred"
                    f["disposition"] = {**answer, "note": answer["note"] + " [fix not verified; deferred]"}
            elif answer["action"] == "defer":
                f["status"] = "deferred"
            elif answer["action"] == "dispute":
                ruling = adjudications.get(f["id"])
                if ruling:
                    f["adjudication"] = ruling
                    if ruling["ruling"] == "REJECT":
                        f["status"] = "refuted-by-adjudication"
        if round_no not in f["rounds_seen"]:
            f["rounds_seen"] = sorted(set(f["rounds_seen"]) | {round_no})
        out.append(f)
    return out


# Lane categories that describe broken behavior. A new finding of one of these on lines the author's
# fix added is a regression of the fix and blocks at any severity once verified. Before this rule, 4 of
# 62 benchmark loop runs (P04, P03, P12, P20) approved with such a confirmed minor regression open.
FIX_REGRESSION_CATEGORIES = (
    "correctness",
    "regression",
    "behavior-change",
    "contract",
    "error-handling",
    "concurrency",
    "resource",
    "security",
)


def in_delta(finding: Mapping[str, Any], delta: Mapping[str, diffs.FileDiff]) -> bool:
    """Whether the finding is introduced by a head line added since the previous round."""
    return any(
        a["side"] == "head" and a["file"] in delta and a["line"] in delta[a["file"]].added
        for a in finding["introduced_by"]
    )


def fix_regression(finding: Mapping[str, Any], delta: Mapping[str, diffs.FileDiff]) -> bool:
    """Whether a lane finding first reported this round is a behavior defect in the author's fix."""
    return finding["origin"] == "lane" and finding["category"] in FIX_REGRESSION_CATEGORIES and in_delta(finding, delta)


def fix_regression_ids(lane_findings: Sequence[Mapping[str, Any]], delta: Mapping[str, diffs.FileDiff]) -> list[str]:
    return sorted(str(f["id"]) for f in lane_findings if fix_regression(f, delta))


def late_or_suppressed(
    new: dict[str, Any],
    carried: Sequence[Mapping[str, Any]],
    delta: Mapping[str, diffs.FileDiff],
) -> str | None:
    """'suppressed', 'late' or None for a lane finding first reported in round N+1."""
    introduced = bool(new.get("replay")) and new["replay"]["classification"] == "introduced"
    for old in carried:
        if old["status"] not in ("refuted", "refuted-by-adjudication"):
            continue
        same = old["fingerprint"] == new["fingerprint"] and old["failure_scenario"] == new["failure_scenario"]
        loc = new["location"]
        unchanged = loc["file"] not in delta or not (
            set(range(loc["start_line"], loc["end_line"] + 1)) & set(delta[loc["file"]].added)
        )
        if same and unchanged and not introduced:
            return "suppressed"
    if not in_delta(new, delta) and not (new["severity"] in ("critical", "major") and introduced):
        return "late"
    return None


def merge_new_findings(
    carried: list[dict[str, Any]], new: Sequence[dict[str, Any]], delta: Mapping[str, diffs.FileDiff]
) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """Fold this round's lane findings into the carried ones (same defect → one finding), and
    apply the late-finding and suppression rules to the rest."""
    notes: dict[str, list[str]] = {}
    merged = list(carried)
    for f in new:
        twin = next((c for c in merged if normalize.similar(c, f)), None)
        if twin is not None:
            normalize.merge_into(twin, f)
            continue
        verdict = late_or_suppressed(f, carried, delta)
        if verdict == "suppressed":
            f = {**f, "bucket": "suppressed", "status": "suppressed"}
            notes[f["id"]] = ["suppressed: the same refuted finding, on unchanged code, with no new executed evidence"]
        elif verdict == "late":
            f = {**f, "late": True}
            notes[f["id"]] = ["late finding on code unchanged since the previous round: counted only as non-blocking"]
        elif fix_regression(f, delta):
            f = {**f, "fix_regression": True}
        merged.append(f)
    return sorted(merged, key=fmod.sort_key), notes
