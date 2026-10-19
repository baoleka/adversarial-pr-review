"""REPLAY, VERIFY, SYNTHESIZE and VERDICT: replay repros in isolation, verify findings in fresh
contexts, compute confidence and buckets, and decide and report the verdict."""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import briefs
import diffs
import lanes
import report
import synthesize
import telemetry
import verdict as verdict_mod
import verify
from engine import Ctx, Step, phase
from lanes import LaneSpec
from phases_lanes import head_commit, pool_step, rnd

import ledger as ledger_mod
import snapshot as snap
from common import SCHEMAS_DIR, SCRIPTS_DIR, ReviewError, atomic_write_text, git, read_json, run, write_json
from jsonschema_lite import validate

PROTECTED_DROP = {"breaker"}


def load_store(ctx: Ctx) -> tuple[dict[str, Any], dict[str, Any]]:
    store = read_json(ctx.run.dir / "findings.json")
    return store, store["rounds"][str(ctx.round)]


# ---------------------------------------------------------------- REPLAY


def added_text(ctx: Ctx) -> str:
    file_diffs = diffs.diff_between(ctx.repo, ctx.state["merge_base"], head_commit(ctx))
    return "\n".join(text for fd in file_diffs.values() for _, text in sorted(fd.added.items()))


@phase("REPLAY")
def replay_phase(ctx: Ctx) -> Step:
    store, rstore = load_store(ctx)
    with_repro = [f for f in rstore.get("lane", []) if f["evidence"]["repro"]]
    if not with_repro:
        return Step.advance()
    ps = ctx.phase_state()
    cfg = ctx.config()
    if "job" not in ps:
        work = ctx.run.round_dir() / "replay"
        work.mkdir(parents=True, exist_ok=True)
        atomic_write_text(work / "added.txt", added_text(ctx))
        out = work / "results.json"
        ps["out"] = str(out)
        timeout = min(3600, len(with_repro) * 4 * int(cfg["replay"]["timeout_s"]) + 120)
        argv = [sys.executable, "-E", "-s", str(SCRIPTS_DIR / "replay.py"), "batch", "--run", str(ctx.run.dir)]
        argv += ["--round", str(ctx.round), "--out", str(out), "--added", str(work / "added.txt")]
        ps["job"] = ctx.run.spawn_job("replay", argv, ctx.run.dir, dict(os.environ), timeout, meta={"kind": "replay"})
        return Step.wait([ps["job"]])
    if ctx.run.poll_job(ps["job"]) == "running":
        return Step.wait([ps["job"]])
    out = Path(ps["out"])
    results = read_json(out) if out.is_file() else {}
    if not out.is_file():
        tail = ctx.run.job_output(ps["job"], "stderr")[-500:]
        rnd(ctx)["replay_problem"] = f"replay batch {ctx.state['jobs'][ps['job']]['status']}: {tail}"
    for finding in rstore["lane"]:
        if finding["id"] in results:
            finding["replay"] = results[finding["id"]]
        elif finding["evidence"]["repro"]:
            none = {"outcome": "not-run", "signature": None, "exit_code": None, "output_ref": None, "runs": 0}
            finding["replay"] = {
                "head": none,
                "base": dict(none),
                "classification": "not-replayed",
                "reason": "the replay batch did not finish",
            }
    ctx.run.write_state_file("findings.json", store)
    return Step.advance()


# ---------------------------------------------------------------- VERIFY


def replay_section(ctx: Ctx, lane_id: str) -> str:
    if ctx.state.get("replay_isolation") != "docker":
        return "## Running repros\n\nRepro replay is unavailable on this machine; rule from the code and the citations."
    from string import Template

    from common import PROMPTS_DIR

    return Template((PROMPTS_DIR / "replay-section.md").read_text(encoding="utf-8")).substitute(
        replay_cmd=lanes.replay_command(ctx.run, lane_id), replay_timeout=int(ctx.config()["replay"]["timeout_s"])
    )


def _plan_verification(ctx: Ctx, cfg: dict[str, Any], lane_findings: list[dict[str, Any]]) -> dict[str, Any]:
    always: set[str] = set()
    if ctx.round > 1:
        import phases_loop

        always = set(phases_loop.fix_regression_ids(lane_findings, phases_loop.delta_lines(ctx)))
    chosen, _skipped = verify.select(lane_findings, int(cfg["limits"]["verify_cap"]), always)
    entries: dict[str, Any] = {}
    vcfg = cfg["lanes"]["verifier"]
    for index, (file, group) in enumerate(verify.batches(chosen).items(), start=1):
        lane_id = f"r{ctx.round}-verifier-{index}"
        refs = {f"C{i}": f for i, f in enumerate(group, start=1)}
        diff_text = briefs.unit_diff(ctx.repo, ctx.state["merge_base"], head_commit(ctx), [file]).text
        brief = verify.render(
            run_id=ctx.state["run_id"],
            lane_id=lane_id,
            file=file,
            intent=ctx.state.get("intent") or "",
            head_root=ctx.run.wt_head,
            base_root=ctx.run.wt_base,
            scratch=lanes.scratch_dir(ctx.run, lane_id),
            out_path=lanes.out_path(ctx.run, lane_id),
            replay_section=replay_section(ctx, lane_id),
            diff=diff_text,
            refs=refs,
        )
        _, system = briefs.agent_system_text("review-verifier")
        spec = LaneSpec(
            id=lane_id,
            kind="verifier",
            agent="review-verifier",
            model=vcfg["model"],
            effort=vcfg["effort"],
            max_turns=int(vcfg["max_turns"]),
            timeout_s=int(vcfg["timeout_s"]),
            lens="verifier",
            unit=None,
            output_kind="verifier",
            round=ctx.round,
            estimate_usd=telemetry.estimate_call(
                cfg, "verifier", vcfg["model"], vcfg["effort"], telemetry.tokens_of(brief + system)
            ),
            files=[file],
        )
        hashes = lanes.prepare(ctx.run, spec, brief, system)
        major = any(f["severity"] in verify.VERIFY_ALL or f["id"] in always for f in group)
        entries[lane_id] = {
            "spec": spec.to_json(),
            "status": "pending",
            "job": None,
            "hashes": hashes,
            "refs": {ref: f["id"] for ref, f in refs.items()},
            "required": major,
        }
    # Budget: minor-only batches go first; a critical or major batch that can't fit escalates.
    remaining = telemetry.remaining(ctx.state, cfg)
    need = sum(e["spec"]["estimate_usd"] for e in entries.values() if e["required"])
    optional = sorted((e for e in entries.values() if not e["required"]), key=lambda e: e["spec"]["id"], reverse=True)
    total = need + sum(e["spec"]["estimate_usd"] for e in optional)
    for e in optional:
        if total <= remaining:
            break
        e["status"] = "dropped-budget"
        total -= e["spec"]["estimate_usd"]
    if need > remaining:
        ids = sorted(fid for e in entries.values() if e["required"] for fid in e["refs"].values())
        ctx.escalate(
            "budget",
            f"verifying the critical and major findings needs about ${need:.2f}; ${remaining:.2f} remains",
            finding_ids=ids,
            decision="Raise the budget (--budget) to verify them.",
        )
        for e in entries.values():
            e["status"] = "dropped-budget"
    return entries


def verifier_replays(ctx: Ctx, lane_id: str) -> list[str]:
    root = ctx.run.dir / "replays" / "lanes" / lane_id
    return sorted(str(p) for p in root.glob("*/record.json")) if root.is_dir() else []


@phase("VERIFY")
def verify_phase(ctx: Ctx) -> Step:
    cfg = ctx.config()
    r = rnd(ctx)
    store, rstore = load_store(ctx)
    lane_findings: list[dict[str, Any]] = rstore.get("lane", [])
    ps = ctx.phase_state()
    if "planned" not in ps:
        ps["planned"] = True
        r["verify"] = _plan_verification(ctx, cfg, lane_findings) if lane_findings else {}
    entries: dict[str, Any] = r["verify"]
    running = pool_step(ctx, cfg, entries, "verify", reserve=0.0)
    if running:
        return Step.wait(running, any_done=True)
    by_id = {f["id"]: f for f in lane_findings}
    notes: dict[str, str] = rstore.setdefault("verifier_notes", {})
    roots = {"head": ctx.run.wt_head, "base": ctx.run.wt_base}
    for lane_id in sorted(entries):
        entry = entries[lane_id]
        if entry["status"] != "ok":
            continue
        refs_ids = entry["refs"] if "refs" in entry else entries[entry["retry_of"]]["refs"]
        spec = LaneSpec.from_json(entry["spec"])
        payload, problem = lanes.load_output(ctx.run, spec)
        if problem or payload is None:
            continue
        refs = {ref: by_id[fid] for ref, fid in refs_ids.items() if fid in by_id}
        notes.update(
            verify.apply_rulings(
                refs, payload, roots, {"model": spec.model, "call_id": spec.id}, verifier_replays(ctx, spec.id)
            )
        )
    ctx.run.write_state_file("findings.json", store)
    return Step.advance()


# ---------------------------------------------------------------- SYNTHESIZE


def round_lane_findings(ctx: Ctx, rstore: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, list[str]]]:
    """This round's lane findings; from round 2 on, folded into the previous round's findings."""
    if ctx.round == 1:
        return list(rstore.get("lane", [])), {}
    import phases_loop

    r = rnd(ctx)
    prior = phases_loop.prev_final(ctx)
    carried = phases_loop.carry_over(
        prior, phases_loop.dispositions(ctx), r.get("fixes", {}), r.get("adjudications", {}), ctx.round
    )
    return phases_loop.merge_new_findings(carried, rstore.get("lane", []), phases_loop.delta_lines(ctx))


def run_synthesis(ctx: Ctx, rstore: dict[str, Any]) -> synthesize.Synthesis:
    cfg = ctx.config()
    lane, loop_notes = round_lane_findings(ctx, rstore)
    syn = synthesize.synthesize(
        rstore.get("mechanical", []),
        lane,
        rstore.get("out_of_scope", []),
        rstore.get("below_evidence_bar", []),
        rstore.get("verifier_notes", {}),
        cfg["overrides"].get("known_preexisting", []),
    )
    for fid, extra in loop_notes.items():
        syn.notes.setdefault(fid, []).extend(extra)
    return syn


@phase("SYNTHESIZE")
def synthesize_phase(ctx: Ctx) -> Step:
    r = rnd(ctx)
    store, rstore = load_store(ctx)
    syn = run_synthesis(ctx, rstore)
    rstore["final"] = syn.findings
    rstore["out_of_scope_final"] = syn.out_of_scope
    rstore["notes"] = syn.notes
    if syn.unverified_major:
        ctx.escalate(
            "unverified-critical-major",
            f"{len(syn.unverified_major)} critical, major or fix-regression finding(s) could not be verified",
            finding_ids=syn.unverified_major,
            decision="Rerun with a larger budget, or review these findings yourself.",
        )
    acked = set(ctx.state["args"].get("ack", []))
    integrity_open = [
        f["id"]
        for f in syn.findings
        if f["category"] == "review-integrity" and f["id"] not in acked and f["status"] == "open"
    ]
    if integrity_open:
        ctx.escalate(
            "review-integrity-open",
            "the change edits the review's own configuration or gates",
            finding_ids=integrity_open,
            decision="Review those edits yourself; rerun with --ack <id> to accept them.",
        )
    # The author's tree must be exactly as it was when the round's snapshot was taken.
    cap = int(ctx.config()["limits"]["untracked_size_cap"])
    after, _ = snap.snapshot_tree(ctx.repo, ctx.run.dir / "tmp", ctx.state["run_dir_rel"], cap)
    r["author_tree_after"] = after
    if after != r["author_tree_before"]:
        changed = git(ctx.repo, "diff", "--name-status", r["author_tree_before"], after, check=False)
        ctx.state.setdefault("violations", []).append(
            {"round": ctx.round, "where": "author tree", "detail": changed[:2000]}
        )
        ctx.escalate("integrity", "the author's tree changed during the review:\n" + changed[:1500])
    for root, sha in ((ctx.run.wt_base, ctx.state["merge_base"]), (ctx.run.wt_head, head_commit(ctx))):
        dirty = snap.worktree_dirty(root)
        if dirty:
            ctx.state.setdefault("violations", []).append(
                {"round": ctx.round, "where": str(root), "detail": dirty[:500]}
            )
            snap.reset_worktree(root, sha)
    ctx.run.write_state_file("findings.json", store)
    return Step.advance()


# ---------------------------------------------------------------- VERDICT


def coverage(ctx: Ctx, triage: dict[str, Any]) -> dict[str, Any]:
    changed = int(triage["reviewable_lines"])
    excluded = int(triage["changed_lines"]) - changed
    if not triage["lanes"] or changed == 0:
        return {"changed_lines": changed, "excluded_lines": excluded, "reviewed_lines": changed, "ratio": 1.0}
    file_diffs = diffs.diff_between(ctx.repo, ctx.state["merge_base"], head_commit(ctx))
    # The change is reviewed across rounds: round 1 reviews all of it, later rounds the deltas. A
    # file's changed lines count as reviewed when a lane that succeeded was shown them.
    reviewed: dict[str, int] = {}
    all_lanes = [e for rnd_state in ctx.state["rounds"].values() for e in rnd_state.get("lanes", {}).values()]
    for entry in all_lanes:
        if entry["status"] != "ok":
            continue
        unshown = entry.get("unshown_lines", {})
        for f in entry["spec"]["files"]:
            if f in file_diffs:
                shown = max(0, file_diffs[f].changed_lines - int(unshown.get(f, 0)))
                reviewed[f] = max(reviewed.get(f, 0), shown)
    lines = min(sum(reviewed.values()), changed)
    return {
        "changed_lines": changed,
        "excluded_lines": excluded,
        "reviewed_lines": lines,
        "ratio": round(lines / changed, 4),
    }


def protected_dropped(ctx: Ctx, triage: dict[str, Any]) -> list[str]:
    dropped = []
    sensitive = bool(triage["sensitive_files"] or triage["security_hits"])
    for lane_id, entry in rnd(ctx).get("lanes", {}).items():
        kind = entry["spec"]["kind"]
        if entry["status"] != "dropped-budget":
            continue
        if (
            kind in PROTECTED_DROP
            or (kind == "security" and sensitive)
            or (kind == "contracts" and triage["public_interface_changes"])
        ):
            dropped.append(lane_id)
    return sorted(dropped)


def call_rows(ctx: Ctx) -> list[dict[str, Any]]:
    stats = rnd(ctx).get("lane_stats", {})
    rows = []
    for call in ctx.state.get("calls", []):
        if call["round"] != ctx.round:
            continue
        s = stats.get(call["id"], {})
        rows.append(
            {
                "id": call["id"],
                "lane": call["lane"],
                "model": call["model"],
                "effort": call["effort"],
                "status": call["status"],
                "findings_raw": s.get("raw"),
                "kept": s.get("kept"),
                "rejected": s.get("rejected"),
                "cost_usd": call["cost_usd"],
                "tokens": call["tokens"],
            }
        )
    return sorted(rows, key=lambda c: c["id"])


def toolchain(ctx: Ctx) -> dict[str, Any]:
    versions: dict[str, Any] = {"python": sys.version.split()[0]}
    versions["git"] = run(["git", "--version"], check=False).stdout.strip()
    inits = [e.get("result", {}).get("init") for e in rnd(ctx).get("lanes", {}).values()]
    versions["claude"] = next((i["claude_code_version"] for i in inits if i and i.get("claude_code_version")), None)
    return versions


def pending_fixes(fixes: Mapping[str, Any], found: list[dict[str, Any]]) -> list[str]:
    """Claimed fixes that must still be verified before APPROVE: those of critical and major
    findings, and of regressions of an earlier fix. Another minor or nit finding whose fix didn't
    verify is deferred instead (P06/P07 of the first held-out run looped to the round cap on a
    recomputed minor coverage finding)."""
    binding = {f["id"]: f["severity"] in ("critical", "major") or bool(f.get("fix_regression")) for f in found}
    return sorted(fid for fid, fx in fixes.items() if fx["status"] != "fixed" and binding.get(fid, True))


def blocking_fingerprints(found: list[dict[str, Any]]) -> list[str]:
    return sorted(
        f["fingerprint"] for f in found if f["bucket"] == "blocking" and f["status"] in verdict_mod.OPEN_STATUSES
    )


def loop_stop(ctx: Ctx, found: list[dict[str, Any]], ledger: dict[str, Any]) -> tuple[str, str, list[str]] | None:
    """An ESCALATE condition that ends the loop instead of another author round, if any."""
    cfg = ctx.config()
    if ctx.round >= int(cfg["limits"]["max_rounds"]):
        return "round-cap", f"another round is needed after {ctx.round} rounds (max_rounds)", blocking_ids(found)
    reopened = ledger_mod.reopened_twice(ledger)
    if reopened:
        ids = sorted({fid for fp in reopened for fid in ledger["fingerprints"][fp]["finding_ids"]})
        return "reopened-twice", "a finding was reopened twice", ids
    if ctx.round > 1:
        import phases_loop

        before = blocking_fingerprints(phases_loop.prev_final(ctx))
        now = blocking_fingerprints(found)
        if now and now == before:
            return (
                "blocking-unchanged-2-rounds",
                "the same findings block after the author's round",
                blocking_ids(found),
            )
    return None


def blocking_ids(found: list[dict[str, Any]]) -> list[str]:
    return [
        f["id"]
        for f in found
        if f["bucket"] in ("blocking", "must-address") and f["status"] in verdict_mod.OPEN_STATUSES
    ]


def author_payload(ctx: Ctx, found: list[dict[str, Any]]) -> dict[str, Any]:
    import dispositions

    must = dispositions.to_answer(found)
    optional = dispositions.optional(found)

    def item(f: Mapping[str, Any]) -> dict[str, Any]:
        loc = f["location"]
        repro = f["evidence"]["repro"]
        replay = f.get("replay") or {}
        return {
            "id": f["id"],
            "severity": f["severity"],
            "bucket": f["bucket"],
            "status": f["status"],
            "origin": f["origin"],
            "can_dispute": f["origin"] == "lane",
            "title": f["title"],
            "where": f"{loc['file']}:{loc['start_line']}" + ("" if loc["side"] == "head" else " (base)"),
            "when": f["failure_scenario"]["conditions"],
            "expected": f["failure_scenario"]["expected"],
            "actual": f["failure_scenario"]["actual"],
            "repro": {
                "file": repro["filename"],
                "kind": repro["kind"],
                "head": (replay.get("head") or {}).get("outcome"),
            }
            if repro
            else None,
            "suggested_fix": f["suggested_fix"],
        }

    return {
        "say": f"{len(must)} finding(s) to fix or dispute; {len(optional)} minor or nit finding(s) you may fix or defer",
        "findings": [item(f) for f in must],
        "optional": [
            {"id": f["id"], "severity": f["severity"], "title": f["title"], "where": item(f)["where"]} for f in optional
        ],
        "author_list": str(ctx.run.round_dir() / "author.md"),
    }


def verdict_data(
    ctx: Ctx, cfg: dict[str, Any], decision: verdict_mod.Decision, found: list[dict[str, Any]], cov: dict[str, Any]
) -> dict[str, Any]:
    r = rnd(ctx)
    budget = telemetry.budget_limit(cfg, ctx.state["mode"], ctx.state["args"].get("budget_usd"))
    by_kind: dict[str, float] = {}
    for call in ctx.state.get("calls", []):
        by_kind[call["kind"]] = round(by_kind.get(call["kind"], 0.0) + float(call["cost_usd"]), 4)
    violations = [
        {"kind": "author-tree" if v["where"] == "author tree" else "worktree", "detail": v["detail"][:500]}
        for v in ctx.state.get("violations", [])
    ]
    escalations = ctx.state.get("escalations", []) if decision.verdict == "ESCALATE" else []
    integrity_ok = not violations and not any(e["condition"] == "integrity" for e in ctx.state.get("escalations", []))
    fixes = r.get("fixes", {})
    return {
        "schema_version": "1",
        "run_id": ctx.state["run_id"],
        "round": ctx.round,
        "mode": ctx.state["mode"],
        "verdict": "REQUEST_CHANGES" if decision.verdict == "AUTHOR" else decision.verdict,
        "reason": decision.reason,
        "escalations": [
            {
                k: e.get(k, [] if k == "finding_ids" else "")
                for k in ("condition", "finding_ids", "detail", "decision_needed")
            }
            for e in escalations
        ],
        "blocking_ids": synthesize.by_bucket(found, "blocking"),
        "must_address_ids": synthesize.by_bucket(found, "must-address"),
        "contested_ids": synthesize.by_bucket(found, "contested"),
        "open_fix_ids": sorted(fid for fid, fx in fixes.items() if fx["status"] != "fixed"),
        "gate_summary": r.get("gate_summary", {}),
        "lanes": call_rows(ctx),
        "coverage": cov,
        "cost": {"total_usd": telemetry.spent(ctx.state), "budget_usd": budget, "by_kind": by_kind},
        "snapshot_tree": r["snapshot"]["tree"],
        "snapshot_commit": r["snapshot"]["commit"],
        "merge_base": ctx.state["merge_base"],
        "base_ref": ctx.state["base_ref"],
        "single_family": not ctx.state.get("external_ok"),
        "stale_base": ctx.state.get("stale_base"),
        "integrity": {"ok": integrity_ok, "violations": violations},
        "replay_isolation": ctx.state.get("replay_isolation") or "none",
        "toolchain": toolchain(ctx),
        "state_hashes": {},
    }


@phase("VERDICT")
def verdict_phase(ctx: Ctx) -> Step:
    cfg = ctx.config()
    r = rnd(ctx)
    store, rstore = load_store(ctx)
    if "final" not in rstore:
        syn = run_synthesis(ctx, rstore)
        rstore.update({"final": syn.findings, "out_of_scope_final": syn.out_of_scope, "notes": syn.notes})
    found: list[dict[str, Any]] = rstore["final"]
    triage = read_json(ctx.run.round_dir() / "triage.json")
    cov = coverage(ctx, triage)
    if cov["ratio"] < 1.0 and not ctx.state.get("escalations"):
        skipped = [u for rs in ctx.state["rounds"].values() for u in rs.get("skipped_units", [])]
        why = f" ({len(skipped)} review unit(s) over max_review_lines)" if skipped else ""
        ctx.escalate(
            "coverage<100",
            f"only {cov['reviewed_lines']} of {cov['changed_lines']} changed lines were reviewed{why}",
            decision="Split the change into smaller pieces and review each.",
        )
    if ctx.state.get("unimplemented_phases"):
        ctx.escalate("degraded", "pipeline phases not implemented: " + ", ".join(ctx.state["unimplemented_phases"]))
    ledger = read_json(ctx.run.dir / "ledger.json")
    ledger_mod.record_round(ledger, ctx.round, found)
    ctx.run.write_state_file("ledger.json", ledger)
    fixes_pending = pending_fixes(r.get("fixes", {}), found)
    decision = verdict_mod.decide(
        ctx.state["mode"],
        ctx.state.get("escalations", []),
        found,
        cov["ratio"],
        lanes_ok=not r.get("lane_summary", {}).get("failed"),
        protected_dropped=protected_dropped(ctx, triage),
        fixes_pending=fixes_pending,
    )
    if decision.verdict == "AUTHOR":
        stop = loop_stop(ctx, found, ledger)
        if stop:
            condition, detail, ids = stop
            ctx.escalate(
                condition,
                detail,
                finding_ids=ids,
                decision="Decide whether to keep iterating, merge as is, or rework the change.",
            )
            decision = verdict_mod.Decision("ESCALATE", f"escalation: {condition}")
    elif decision.verdict == "ESCALATE" and not ctx.state.get("escalations"):
        # An APPROVE condition failed: record it like any other escalation.
        for e in decision.escalations:
            ctx.escalate(e["condition"], e["detail"])
    if ctx.state.get("escalations") and decision.verdict != "ESCALATE":
        conditions = sorted({e["condition"] for e in ctx.state["escalations"]})
        decision = verdict_mod.Decision("ESCALATE", "escalation: " + ", ".join(conditions))
    data = verdict_data(ctx, cfg, decision, found, cov)
    ctx.run.write_state_file("findings.json", store)
    data["state_hashes"] = {k: v for k, v in sorted(ctx.run.recorded_hashes().items()) if k != "verdict.json"}
    problems = validate(data, read_json(SCHEMAS_DIR / "verdict.schema.json"))
    if problems:
        raise ReviewError("internal: invalid verdict: " + "; ".join(problems[:5]))
    write_json(ctx.run.round_dir() / "verdict.json", data)
    atomic_write_text(
        ctx.run.dir / "report.md", report.render(data, rstore, rstore.get("notes", {}), ctx.state.get("intent") or "")
    )
    atomic_write_text(ctx.run.round_dir() / "author.md", report.author_list(found))
    if decision.verdict == "AUTHOR":
        return Step.author(author_payload(ctx, found))
    ctx.run.write_state_file("verdict.json", data)
    return Step.terminal(decision.verdict)
