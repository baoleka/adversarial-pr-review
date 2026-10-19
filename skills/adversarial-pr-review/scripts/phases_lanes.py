"""BRIEFS, PROBE, LANES and NORMALIZE: render briefs, prove the lane configuration is confined,
run the review lanes headless within budget, and normalize what they report."""

from __future__ import annotations

import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

import briefs
import diffs
import external
import findings as fmod
import interface
import lanes
import normalize
import replay
import telemetry
from engine import Ctx, Step, phase
from lanes import LaneSpec

import snapshot as snap
from common import SCRIPTS_DIR, ReviewError, read_json, write_json

AGENT_FOR = {
    "breaker": "review-breaker",
    "external": "review-breaker",
    "contracts": "review-contracts",
    "tests": "review-tests",
    "security": "review-security",
    "delta": "review-delta",
    "interface": "review-contracts",
}
LENS_FOR = {"external": "second-lens"}
PRIORITY = {"breaker": 0, "delta": 0, "security": 1, "external": 2, "contracts": 3, "tests": 4, "interface": 5}
VERIFY_RESERVE_CALLS = 2


def rnd(ctx: Ctx) -> dict[str, Any]:
    data: dict[str, Any] = ctx.state["rounds"][str(ctx.round)]
    return data


def head_commit(ctx: Ctx) -> str:
    return str(rnd(ctx)["snapshot"]["commit"])


def claude_bin(cfg: dict[str, Any]) -> str:
    return str(cfg["limits"].get("claude_bin", "claude"))


def record_call(ctx: Ctx, spec: LaneSpec, result: lanes.LaneResult, kind: str) -> None:
    ctx.state.setdefault("calls", []).append(
        {
            "id": spec.id,
            "kind": kind,
            "lane": spec.kind,
            "model": spec.model,
            "effort": spec.effort,
            "round": ctx.round,
            "status": result.status,
            "cost_usd": result.cost_usd,
            "measured": result.cost_measured,
            "tokens": {k: result.usage.get(k, 0) for k in ("input", "output", "cache_read", "cache_write")},
            "turns": result.usage.get("turns", 0),
        }
    )


# ---------------------------------------------------------------- BRIEFS


def _units_for(kind: str, units: list[dict[str, Any]], triage: dict[str, Any]) -> list[dict[str, Any]]:
    """Breaker and second lens review every unit; specialist lanes the units that need them."""
    if kind in ("breaker", "external") or len(units) <= 1:
        return units
    if kind == "security":
        hits = triage["security_hits"] + triage.get("injection_hits", [])
        hot = set(triage["sensitive_files"]) | {h.rsplit(":", 1)[0] for h in hits}
    elif kind == "tests":
        hot = set(triage["classes"].get("tests", []))
    else:
        hot = {c.split(":", 1)[0] for c in triage["public_interface_changes"]}
    picked = [u for u in units if hot & set(u["files"])]
    return picked or units[:1]


def lane_plan(
    ctx: Ctx, cfg: dict[str, Any], triage: dict[str, Any]
) -> list[tuple[str, str, list[str], briefs.CappedDiff]]:
    """(lane kind, unit id, files, diff, truncated files) for each lane of this round.

    Round 1 reviews the whole change. Later rounds review the delta since the previous round with
    one delta lane, or the whole change again when the delta is a large share of it."""
    base, head = ctx.state["merge_base"], head_commit(ctx)
    plan: list[tuple[str, str, list[str], briefs.CappedDiff]] = []
    if ctx.round > 1:
        previous = ctx.state["rounds"][str(ctx.round - 1)]["snapshot"]["commit"]
        delta = diffs.diff_between(ctx.repo, previous, head)
        delta_lines = sum(fd.changed_lines for fd in delta.values())
        rnd(ctx)["delta_lines"] = delta_lines
        if delta_lines == 0:
            return []
        ratio = delta_lines / max(1, int(triage["reviewable_lines"]))
        if ratio <= float(cfg["limits"]["full_rereview_delta_ratio"]):
            files = sorted(delta)
            return [
                ("delta", "d1", files, briefs.cap_diff(diffs.function_context_diff(ctx.repo, previous, head, files)))
            ]
    units = review_units(ctx, cfg, triage)
    for kind in sorted(triage["lanes"], key=lambda k: PRIORITY.get(k, 9)):
        for unit in _units_for(kind, units, triage):
            plan.append((kind, unit["id"], unit["files"], briefs.unit_diff(ctx.repo, base, head, unit["files"])))
    changes = triage["public_interface_changes"]
    if len(units) > 1 and changes and cfg["lanes"].get("interface", {}).get("enabled", True):
        # One pass over changed public signatures and their callers outside the diff.
        text, _callers = interface.summary(ctx.repo, base, head, changes, triage["changed_files"])
        files = sorted({c.split(":", 1)[0] for c in changes})
        capped = briefs.unit_diff(ctx.repo, base, head, files)
        capped.text = f"Changed public names:\n\n{text}\n\n{capped.text}"
        plan.append(("interface", "i1", files, capped))
    return plan


def review_units(ctx: Ctx, cfg: dict[str, Any], triage: dict[str, Any]) -> list[dict[str, Any]]:
    """The riskiest units that fit in `max_review_lines` (units come in risk order). Skipped units
    leave coverage below 100%, so the run escalates with a recommendation to split the change."""
    cap = int(cfg["limits"]["max_review_lines"])
    chosen: list[dict[str, Any]] = []
    total = 0
    for unit in triage["units"]:
        if chosen and total + int(unit["changed_lines"]) > cap:
            continue
        chosen.append(unit)
        total += int(unit["changed_lines"])
    rnd(ctx)["skipped_units"] = [u["id"] for u in triage["units"] if u not in chosen]
    return chosen


@phase("BRIEFS")
def briefs_phase(ctx: Ctx) -> Step:
    pull = ctx.state.get("image_pull")
    if pull and ctx.run.poll_job(pull["job"]) == "running":
        # Replay isolation is decided below, once the image pull started at GATES has finished.
        return Step.wait([pull["job"]])
    cfg = ctx.config()
    r = rnd(ctx)
    triage = read_json(ctx.run.round_dir() / "triage.json")
    r["lanes"] = {}
    mode, problem = replay.isolation_mode(cfg["replay"])
    py = ctx.state.get("replay_python") or {}
    if mode == "docker" and py.get("problem"):
        mode, problem = "none", py["problem"]
    ctx.state["replay_isolation"], ctx.state["replay_problem"] = mode, problem
    if not triage["lanes"]:
        return Step.advance()
    store = read_json(ctx.run.dir / "findings.json")
    mechanical = store["rounds"][str(ctx.round)]["mechanical"]
    base = ctx.state["merge_base"]
    conventions = briefs.conventions_text(ctx.repo, base, cfg["repo"].get("conventions_docs", []))
    names = briefs.checklist_names(
        triage["change_types"],
        tests_changed=bool(triage["classes"].get("tests")),
        security=bool(triage["sensitive_files"] or triage["security_hits"] or triage.get("injection_hits")),
        dependency=bool(triage["dependency_change"]),
    )
    summary: dict[str, Any] = {"gates": r.get("gate_summary", {}), "diff_coverage": r.get("diff_coverage")}
    if triage.get("injection_hits"):
        summary["reviewer_directed_text"] = triage["injection_hits"]
    for kind, unit_id, files, capped in lane_plan(ctx, cfg, triage):
        diff_text, truncated = capped.text, capped.notes
        lane_cfg = cfg["lanes"][kind]
        lane_id = f"r{ctx.round}-{kind}-{unit_id}"
        inputs = briefs.BriefInputs(
            run_id=ctx.state["run_id"],
            lane_id=lane_id,
            lane_kind=kind,
            intent=ctx.state.get("intent") or "",
            change_types=triage["change_types"],
            head_root=ctx.run.wt_head,
            base_root=ctx.run.wt_base,
            scratch=lanes.scratch_dir(ctx.run, lane_id),
            out_path=lanes.out_path(ctx.run, lane_id),
            replay_cmd=lanes.replay_command(ctx.run, lane_id) if mode == "docker" else None,
            replay_timeout=int(cfg["replay"]["timeout_s"]),
            conventions=conventions,
            mechanical=mechanical,
            checks_summary=summary,
            checklist=briefs.load_checklist(names, kind, ctx.state.get("checklist_added", [])),
            files=files,
            diff=diff_text,
            truncated=truncated,
        )
        brief = briefs.render_lane(inputs)
        _, system = briefs.agent_system_text(AGENT_FOR[kind])
        spec = LaneSpec(
            id=lane_id,
            kind=kind,
            agent=AGENT_FOR[kind],
            model=lane_cfg["model"],
            effort=lane_cfg["effort"],
            max_turns=int(lane_cfg["max_turns"]),
            timeout_s=int(lane_cfg["timeout_s"]),
            lens=LENS_FOR.get(kind, kind),
            unit=unit_id,
            output_kind="findings",
            round=ctx.round,
            estimate_usd=telemetry.estimate_call(
                cfg, kind, lane_cfg["model"], lane_cfg["effort"], telemetry.tokens_of(brief + system)
            ),
            files=files,
        )
        hashes = lanes.prepare(ctx.run, spec, brief, system)
        entry: dict[str, Any] = {
            "spec": spec.to_json(),
            "status": "pending",
            "job": None,
            "hashes": hashes,
            "truncated": truncated,
            "unshown_lines": capped.unshown,
        }
        if kind == "external":
            problem = external.codex_problem(cfg)
            if problem is None:
                export_root = lanes.control_dir(ctx.run, lane_id) / "export"
                codex_inputs = briefs.BriefInputs(
                    **{
                        **inputs.__dict__,
                        "head_root": export_root / "head",
                        "base_root": export_root / "base",
                        "replay_cmd": None,
                    }
                )
                commits = {"head": head_commit(ctx), "base": base}
                entry["runner"] = "codex"
                entry["codex"] = external.prepare(
                    ctx.run, spec, briefs.render_lane(codex_inputs), cfg, commits, ctx.repo
                )
            else:
                ctx.state["single_family_reason"] = problem
        r["lanes"][lane_id] = entry
    return Step.advance()


# ---------------------------------------------------------------- PROBE


def guard_selftest(ctx: Ctx) -> list[str]:
    """Run the real guard script on synthetic payloads; every decision must match."""
    work = ctx.run.dir / "selftest"
    scratch = work / "scratch"
    scratch.mkdir(parents=True, exist_ok=True)
    policy = {
        "lane": "selftest",
        "run": str(ctx.run.dir),
        "scratch": str(scratch),
        "read_roots": [str(ctx.run.wt_head), str(ctx.run.wt_base), str(scratch)],
        "replay": str(SCRIPTS_DIR / "replay.py"),
        "heartbeat": str(work / "guard.log"),
    }
    write_json(work / "policy.json", policy)
    if (work / "guard.log").exists():
        (work / "guard.log").unlink()
    replay_cmd = f"python3 -E -s {SCRIPTS_DIR / 'replay.py'} lane --run {ctx.run.dir} --lane selftest {scratch}/t.py"
    cases = [
        ("Write", {"file_path": str(scratch / "out.json"), "content": "{}"}, True),
        ("Write", {"file_path": str(ctx.run.dir / "state.json"), "content": "{}"}, False),
        ("Write", {"file_path": str(ctx.repo / "x.py"), "content": ""}, False),
        ("Edit", {"file_path": str(scratch / "out.json"), "old_string": "a", "new_string": "b"}, False),
        ("Read", {"file_path": str(ctx.run.wt_head)}, True),
        ("Read", {"file_path": str(Path.home() / ".bashrc")}, False),
        ("Bash", {"command": replay_cmd}, True),
        ("Bash", {"command": f"touch {ctx.repo}/x"}, False),
        ("Bash", {"command": replay_cmd + f" > {ctx.repo}/x"}, False),
        ("WebFetch", {"url": "https://example.com"}, False),
    ]
    env = {**os.environ, "ADREV_LANE_ID": "selftest", "ADREV_LANE_POLICY": str(work / "policy.json")}
    problems = []
    for tool, tool_input, expect in cases:
        event = {"tool_name": tool, "tool_input": tool_input, "cwd": str(scratch), "hook_event_name": "PreToolUse"}
        proc = subprocess.run(
            [sys.executable, "-E", "-s", str(SCRIPTS_DIR / "hooks" / "guard.py")],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        allowed = proc.returncode == 0
        if allowed != expect:
            problems.append(f"guard {'allowed' if allowed else 'denied'} {tool} {json.dumps(tool_input)[:120]}")
    beats = lanes.read_heartbeat(work / "guard.log")
    if len(beats) != len(cases):
        problems.append(f"guard heartbeat recorded {len(beats)} of {len(cases)} calls")
    return problems


def _probe_spec(ctx: Ctx, attempt: int) -> LaneSpec:
    cfg = ctx.config()
    probe_cfg = cfg["lanes"]["probe"]
    return LaneSpec(
        id=f"r{ctx.round}-probe-{attempt}",
        kind="probe",
        agent="review-probe",
        model=probe_cfg["model"],
        effort=probe_cfg["effort"],
        max_turns=int(probe_cfg["max_turns"]),
        timeout_s=int(probe_cfg["timeout_s"]),
        lens="probe",
        unit=None,
        output_kind="probe",
        round=ctx.round,
        estimate_usd=telemetry.estimate_call(cfg, "probe", probe_cfg["model"], probe_cfg["effort"], 300),
        budget_cap_usd=0.5,
        attempt=attempt,
    )


def spawn_probe(ctx: Ctx, attempt: int) -> dict[str, Any]:
    spec = _probe_spec(ctx, attempt)
    nonce = secrets.token_hex(6)
    deny_write = ctx.run.dir / f"probe-{nonce}"
    deny_touch = ctx.repo / f".adrev-probe-{nonce}"
    brief = briefs.render_probe(nonce, deny_write, deny_touch, lanes.out_path(ctx.run, spec.id))
    _, system = briefs.agent_system_text("review-probe")
    lanes.prepare(ctx.run, spec, brief, system)
    job = lanes.spawn(ctx.run, spec, claude_bin(ctx.config()))
    return {
        "spec": spec.to_json(),
        "job": job,
        "nonce": nonce,
        "deny_write": str(deny_write),
        "deny_touch": str(deny_touch),
    }


def probe_outcome(probe: dict[str, Any], result: lanes.LaneResult, beats: list[dict[str, Any]]) -> tuple[str, str]:
    """pass | violation | inconclusive, with the reason."""
    leaked = [p for p in (probe["deny_write"], probe["deny_touch"]) if Path(p).exists()]
    if leaked:
        return "violation", "the probe lane wrote outside its scratch dir: " + ", ".join(leaked)
    if result.integrity:
        return "violation", "; ".join(result.integrity)
    if result.status != "ok":
        return "inconclusive", f"probe lane {result.status}: {result.problem}"
    if not any(b.get("tool") == "Write" and b.get("allowed") for b in beats):
        return "violation", "the guard did not see the probe's allowed write: lane hooks are not active"
    denied_tools = {d.get("tool_name") for d in result.permission_denials}
    denied_tools |= {str(b.get("tool")) for b in beats if b.get("allowed") is False}
    if not {"Write", "Bash"} <= denied_tools:
        return (
            "inconclusive",
            f"the probe did not attempt both denied writes (denied: {sorted(map(str, denied_tools))})",
        )
    return "pass", "both writes outside the scratch dir were denied; the guard and output validator ran"


@phase("PROBE")
def probe_phase(ctx: Ctx) -> Step:
    r = rnd(ctx)
    ps = ctx.phase_state()
    if not r.get("lanes") and ctx.round == 1:
        ps["skipped"] = "no model calls this round"
        return Step.advance()
    if "selftest" not in ps:
        ps["selftest"] = guard_selftest(ctx)
        if ps["selftest"]:
            ctx.escalate("integrity", "guard self-test failed: " + "; ".join(ps["selftest"])[:1500])
            return Step.advance()
    attempts: list[dict[str, Any]] = ps.setdefault("attempts", [])
    if not attempts:
        if not telemetry.can_afford(ctx.state, ctx.config(), _probe_spec(ctx, 1).estimate_usd):
            ctx.escalate("budget", "the budget can't cover the safety probe", decision="Raise the budget (--budget).")
            return Step.advance()
        attempts.append(spawn_probe(ctx, 1))
        return Step.wait([attempts[-1]["job"]])
    current = attempts[-1]
    if ctx.run.poll_job(current["job"]) == "running":
        return Step.wait([current["job"]])
    if "outcome" not in current:
        spec = LaneSpec.from_json(current["spec"])
        result = lanes.collect(ctx.run, spec, current["job"])
        record_call(ctx, spec, result, "probe")
        beats = lanes.read_heartbeat(lanes.control_dir(ctx.run, spec.id) / "guard.log")
        current["outcome"], current["reason"] = probe_outcome(current, result, beats)
        for leftover in (current["deny_write"], current["deny_touch"]):
            if Path(leftover).exists():
                Path(leftover).unlink()
    if current["outcome"] == "inconclusive" and len(attempts) == 1:
        attempts.append(spawn_probe(ctx, 2))
        return Step.wait([attempts[-1]["job"]])
    r["probe"] = {"outcome": current["outcome"], "reason": current["reason"], "attempts": len(attempts)}
    if current["outcome"] != "pass":
        ctx.escalate(
            "integrity",
            f"probe {current['outcome']}: {current['reason']}",
            decision="Lane confinement could not be proven; check the Claude Code version and lane settings.",
        )
    return Step.advance()


# ---------------------------------------------------------------- LANES


def _plan_budget(ctx: Ctx, cfg: dict[str, Any], entries: dict[str, Any], triage: dict[str, Any]) -> None:
    """Drop lanes (tests → contracts unless the public interface changed → second lens) until the
    planned lanes plus a verification reserve fit the remaining budget."""
    verifier = cfg["lanes"]["verifier"]
    reserve = VERIFY_RESERVE_CALLS * telemetry.estimate_call(
        cfg, "verifier", verifier["model"], verifier["effort"], 8000
    )
    remaining = telemetry.remaining(ctx.state, cfg)
    sensitive = bool(triage["sensitive_files"] or triage["security_hits"])
    droppable = ["tests"] + ([] if triage["public_interface_changes"] else ["contracts"]) + ["external"]
    if not sensitive:
        droppable.append("security")

    def planned() -> float:
        return sum(float(e["spec"]["estimate_usd"]) for e in entries.values() if e["status"] == "pending")

    for kind in droppable:
        if planned() + reserve <= remaining:
            break
        for e in sorted(entries.values(), key=lambda e: e["spec"]["id"], reverse=True):
            if e["spec"]["kind"] == kind and e["status"] == "pending" and planned() + reserve > remaining:
                e["status"] = "dropped-budget"
    if planned() + reserve > remaining:
        must = sorted(e["spec"]["id"] for e in entries.values() if e["status"] == "pending")
        ctx.escalate(
            "budget",
            f"the remaining budget (${remaining:.2f}) can't cover the required lanes {', '.join(must)} "
            f"(${planned():.2f}) plus verification (${reserve:.2f})",
            decision="Raise the budget (--budget) or split the change.",
        )
        for e in entries.values():
            if e["status"] == "pending":
                e["status"] = "dropped-budget"
    ctx.phase_state()["reserve"] = reserve


def _retry_entry(ctx: Ctx, entry: dict[str, Any], suffix: str = "-a2", attempt: int = 2) -> dict[str, Any]:
    spec = LaneSpec.from_json(entry["spec"])
    retry = LaneSpec.from_json({**entry["spec"], "id": f"{spec.id}{suffix}", "attempt": attempt, "budget_cap_usd": 0.0})
    control = lanes.control_dir(ctx.run, spec.id)
    brief = (control / "brief.md").read_text(encoding="utf-8")
    brief = brief.replace(str(lanes.scratch_dir(ctx.run, spec.id)), str(lanes.scratch_dir(ctx.run, retry.id)))
    brief = brief.replace(f"--lane {spec.id} ", f"--lane {retry.id} ")
    system = (control / "system.md").read_text(encoding="utf-8")
    hashes = lanes.prepare(ctx.run, retry, brief, system)
    carried = {
        k: v for k, v in entry.items() if k not in ("spec", "status", "job", "hashes", "result", "runner", "codex")
    }
    return {**carried, "spec": retry.to_json(), "status": "pending", "job": None, "hashes": hashes, "retry_of": spec.id}


def pool_step(ctx: Ctx, cfg: dict[str, Any], entries: dict[str, Any], call_kind: str, reserve: float) -> list[str]:
    """Advance a pool of headless calls: collect finished ones (retrying a failure once), then
    spawn pending ones in priority order within the parallelism and budget limits. Returns the
    jobs still running; an empty list means every call has finished or was dropped."""
    # Collect finished lanes; retry a failed lane once.
    for lane_id in list(entries):
        entry = entries[lane_id]
        if entry["status"] != "running" or ctx.run.poll_job(entry["job"]) == "running":
            continue
        spec = LaneSpec.from_json(entry["spec"])
        codex = entry.get("runner") == "codex"
        result = (
            external.collect(ctx.run, spec, entry["job"], cfg) if codex else lanes.collect(ctx.run, spec, entry["job"])
        )
        record_call(ctx, spec, result, call_kind)
        entry["status"] = result.status
        entry["result"] = result.to_json()
        if codex:
            if result.status == "ok":
                ctx.state["external_ok"] = True
            else:
                # Any codex refusal or failure falls back to the Claude second lens.
                ctx.state["single_family_reason"] = result.problem
                fallback = _retry_entry(ctx, entry, suffix="-claude", attempt=1)
                entries[fallback["spec"]["id"]] = fallback
        elif result.integrity:
            ctx.escalate("integrity", f"lane {lane_id}: " + "; ".join(result.integrity)[:1000])
        elif result.status != "ok" and spec.attempt == 1:
            retry = _retry_entry(ctx, entry)
            entries[retry["spec"]["id"]] = retry
    # Spawn pending lanes in priority order within the parallelism and budget limits.
    running = [e for e in entries.values() if e["status"] == "running"]
    limit = int(cfg["limits"]["max_parallel_calls"])
    pending = sorted(
        (e for e in entries.values() if e["status"] == "pending"),
        key=lambda e: (PRIORITY.get(e["spec"]["kind"], 9), e["spec"]["id"]),
    )
    for entry in pending:
        if len(running) >= limit or ctx.state.get("escalations"):
            break
        available = (
            telemetry.remaining(ctx.state, cfg) - reserve - sum(float(e["spec"]["budget_cap_usd"]) for e in running)
        )
        estimate = float(entry["spec"]["estimate_usd"])
        if available < estimate:
            if running:
                break
            entry["status"] = "dropped-budget"
            continue
        entry["spec"]["budget_cap_usd"] = round(min(3 * estimate, available), 2)
        spec = LaneSpec.from_json(entry["spec"])
        if entry.get("runner") == "codex":
            entry["job"] = external.spawn(ctx.run, spec)
        else:
            entry["job"] = lanes.spawn(ctx.run, spec, claude_bin(cfg))
        entry["status"] = "running"
        running.append(entry)
    return [e["job"] for e in running]


@phase("LANES")
def lanes_phase(ctx: Ctx) -> Step:
    cfg = ctx.config()
    r = rnd(ctx)
    entries: dict[str, Any] = r.get("lanes", {})
    if not entries:
        return Step.advance()
    ps = ctx.phase_state()
    triage = read_json(ctx.run.round_dir() / "triage.json")
    if not ps.get("started"):
        ps["started"] = True
        for root in (ctx.run.wt_head, ctx.run.wt_base):
            snap.set_writable(root, False)
        _plan_budget(ctx, cfg, entries, triage)
    running = pool_step(ctx, cfg, entries, "lane", float(ps.get("reserve", 0.0)))
    if running:
        return Step.wait(running, any_done=True)
    for root in (ctx.run.wt_head, ctx.run.wt_base):
        snap.set_writable(root, True)
    _check_lane_coverage(ctx, cfg, entries)
    return Step.advance()


def _check_lane_coverage(ctx: Ctx, cfg: dict[str, Any], entries: dict[str, Any]) -> None:
    final: dict[str, str] = {}
    for lane_id, entry in entries.items():
        original = entry.get("retry_of") or lane_id
        if entry.get("retry_of") or original not in final:
            final[original] = entry["status"]
    failed = sorted(k for k, s in final.items() if s not in ("ok", "dropped-budget"))
    ok = [k for k, s in final.items() if s == "ok"]
    breaker_failed = [k for k in failed if "-breaker-" in k]
    need = min(int(cfg["limits"]["min_successful_lanes"]), len([s for s in final.values() if s != "dropped-budget"]))
    if breaker_failed or len(ok) < need:
        ctx.escalate(
            "degraded-lane",
            f"lanes failed: {', '.join(failed)} ({len(ok)} of {len(final)} succeeded)",
            decision="Check the lane logs in the run dir; rerun when the cause is fixed.",
        )
    rnd(ctx)["lane_summary"] = {"final": final, "failed": failed}


# ---------------------------------------------------------------- NORMALIZE


@phase("NORMALIZE")
def normalize_phase(ctx: Ctx) -> Step:
    r = rnd(ctx)
    entries: dict[str, Any] = r.get("lanes", {})
    store = read_json(ctx.run.dir / "findings.json")
    rstore = store["rounds"][str(ctx.round)]
    kept: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    out_of_scope: list[dict[str, Any]] = []
    below: list[str] = []
    stats: dict[str, Any] = {}
    if entries:
        file_diffs = diffs.diff_between(ctx.repo, ctx.state["merge_base"], head_commit(ctx))
        roots = {"head": ctx.run.wt_head, "base": ctx.run.wt_base}
        for lane_id in sorted(entries):
            entry = entries[lane_id]
            if entry["status"] != "ok":
                continue
            spec = LaneSpec.from_json(entry["spec"])
            payload, problem = lanes.load_output(ctx.run, spec)
            if problem:
                raise ReviewError(f"lane {lane_id} output changed after collection: {problem}")
            meta = normalize.LaneMeta(spec.id, spec.model, spec.lens, spec.unit, f"agents/{spec.id}/out.json")
            res = normalize.normalize_lane(payload, meta, roots, file_diffs, ctx.round)
            kept += res.kept
            rejected += res.rejected
            out_of_scope += res.out_of_scope
            below += res.below_evidence_bar
            stats[lane_id] = {
                "raw": len(payload["findings"]),
                "kept": len(res.kept),
                "rejected": len(res.rejected),
                "out_of_scope": len(res.out_of_scope),
            }
    merged = normalize.dedupe(kept)
    mechanical_fps = {f["fingerprint"] for f in rstore["mechanical"]}
    merged = [f for f in merged if f["fingerprint"] not in mechanical_fps]
    for f in merged + out_of_scope:
        problems = fmod.problems(f)
        if problems:
            raise ReviewError(f"internal: invalid lane finding {f['id']}: {problems[:3]}")
    rstore["lane"] = merged
    rstore["rejected"] = rejected
    rstore["out_of_scope"] = sorted(out_of_scope, key=fmod.sort_key)
    rstore["below_evidence_bar"] = sorted(set(below) & {f["id"] for f in merged})
    ctx.run.write_state_file("findings.json", store)
    r["lane_stats"] = stats
    return Step.advance()
