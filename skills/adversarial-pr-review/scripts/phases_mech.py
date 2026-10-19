"""TRIAGE, GATES and CHECKS: everything mechanical, before any model call."""

from __future__ import annotations

import os
from dataclasses import asdict
from pathlib import Path
from typing import Any

import checks
import diffs
import findings as fmod
import gates
import parsers
import replay
from engine import Ctx, Step, phase
from triage import run_triage

import snapshot as snap
from common import ReviewError, git, read_json, write_json


def round_paths(ctx: Ctx) -> dict[str, Path]:
    rd = ctx.run.round_dir()
    return {
        "dir": rd,
        "triage": rd / "triage.json",
        "gates": rd / "gates" / "results.json",
        "checks": rd / "checks.json",
    }


def head_commit(ctx: Ctx) -> str:
    return str(ctx.state["rounds"][str(ctx.round)]["snapshot"]["commit"])


def store_findings(ctx: Ctx, mechanical: list[dict[str, Any]], appendix: dict[str, Any]) -> None:
    path = ctx.run.dir / "findings.json"
    data = read_json(path) if path.is_file() else {"rounds": {}}
    data.setdefault("rounds", {})[str(ctx.round)] = {"mechanical": mechanical, "appendix": appendix}
    data["current_round"] = ctx.round
    ctx.run.write_state_file("findings.json", data)


@phase("TRIAGE")
def triage_phase(ctx: Ctx) -> Step:
    cfg = ctx.config()
    base, head = ctx.state["merge_base"], head_commit(ctx)
    file_diffs = diffs.diff_between(ctx.repo, base, head)
    result = run_triage(ctx.repo, cfg, base, head, file_diffs, ctx.state.get("intent") or "")
    paths = round_paths(ctx)
    write_json(paths["triage"], result.to_json())
    rnd = ctx.state["rounds"][str(ctx.round)]
    rnd["triage"] = {
        "size": result.size,
        "lanes": result.lanes,
        "units": len(result.units),
        "changed_lines": result.changed_lines,
        "reviewable_lines": result.reviewable_lines,
        "comment_only": result.comment_only,
        "dependency_change": result.dependency_change,
        "public_interface_changes": len(result.public_interface_changes),
        "sensitive_files": result.sensitive_files,
    }
    acked = set(ctx.state["args"].get("ack", []))
    integrity = checks.review_integrity_findings(
        ctx.repo, cfg, base, head, result.changed_files, ctx.state["config_source"]["source"], ctx.round, acked
    )
    rnd["review_inputs_changed"] = gates.differing_review_inputs(ctx.repo, cfg, base, head)
    rnd["integrity_findings"] = integrity
    return Step.advance()


PULL_TIMEOUT_S = 300


def maybe_pull_image(ctx: Ctx, cfg: dict[str, Any]) -> None:
    """Start pulling the pinned replay image in the background when replays could run here but the
    image is missing; BRIEFS waits for the pull. Round 1 only. A failed pull leaves replays off."""
    if ctx.round != 1 or "image_pull" in ctx.state:
        return
    problem = (ctx.state.get("replay_python") or {}).get("problem")
    if not replay.pull_needed(cfg["replay"], problem):
        return
    image = str(cfg["replay"]["image"])
    job = ctx.run.spawn_job(
        "pull", replay.pull_argv(image), ctx.run.dir, dict(os.environ), PULL_TIMEOUT_S, meta={"image": image}
    )
    ctx.state["image_pull"] = {"job": job, "image": image}


def _spawn_all(ctx: Ctx) -> list[str]:
    cfg = ctx.config()
    rnd = ctx.state["rounds"][str(ctx.round)]
    base_root, head_root = ctx.run.wt_base, ctx.run.wt_head
    for root, sha in ((base_root, ctx.state["merge_base"]), (head_root, head_commit(ctx))):
        dirty = snap.worktree_dirty(root)
        if dirty:
            ctx.state.setdefault("violations", []).append(
                {"round": ctx.round, "where": str(root), "detail": dirty[:500]}
            )
            snap.reset_worktree(root, sha)
    for root in (base_root, head_root):
        problems = gates.import_path_problems(cfg, root)
        if problems:
            raise ReviewError("import path check failed: " + "; ".join(problems))
    if rnd.get("review_inputs_changed"):
        gates.overlay_base_inputs(ctx.repo, ctx.state["merge_base"], head_root, rnd["review_inputs_changed"])
    toolchain = gates.toolchain_fingerprint(cfg, ctx.repo, ctx.state["merge_base"])
    ctx.state["replay_python"] = asdict(replay.describe_python())
    maybe_pull_image(ctx, cfg)
    cache_dir = ctx.run.dir.parent / "base-cache"
    jobs: list[str] = []
    cached: dict[str, str] = {}
    for gate in cfg["repo"]["gates"]:
        key = gates.base_cache_key(
            gate, git(ctx.repo, "rev-parse", f"{ctx.state['merge_base']}^{{tree}}").strip(), toolchain, cfg
        )
        if gates.load_base_cache(cache_dir, key) is not None:
            cached[gate["name"]] = key
        else:
            jobs.append(gates.spawn(ctx.run, cfg, gate, "base", base_root))
        jobs.append(gates.spawn(ctx.run, cfg, gate, "head", head_root))
    ignore = cfg["repo"]["checks"].get("ignore_codes", {})
    if ignore.get("enabled"):
        for name, key, parser in (("ignore-ruff", "ruff_argv", "ruff"), ("ignore-mypy", "mypy_argv", "mypy")):
            if ignore.get(key):
                pseudo = {"name": name, "argv": ignore[key], "parser": parser, "timeout_s": 900}
                jobs.append(gates.spawn(ctx.run, cfg, pseudo, "head", head_root))
    ctx.phase_state()["toolchain"] = toolchain
    ctx.phase_state()["cached"] = cached
    return jobs


@phase("GATES")
def gates_phase(ctx: Ctx) -> Step:
    ps = ctx.phase_state()
    if "jobs" not in ps:
        ps["jobs"] = _spawn_all(ctx)
        if ps["jobs"]:
            return Step.wait(ps["jobs"])
    if any(ctx.run.poll_job(j) == "running" for j in ps["jobs"]):
        return Step.wait([j for j in ps["jobs"] if ctx.state["jobs"][j]["status"] == "running"])
    cfg = ctx.config()
    cache_dir = ctx.run.dir.parent / "base-cache"
    results: dict[str, dict[str, Any]] = {}
    head_tree = git(ctx.repo, "rev-parse", f"{ctx.state['merge_base']}^{{tree}}").strip()
    for job_id in ps["jobs"]:
        job = ctx.state["jobs"][job_id]
        name, side = job["meta"]["gate"], job["meta"]["side"]
        spec = next((g for g in cfg["repo"]["gates"] if g["name"] == name), None) or {
            "name": name,
            "parser": "ruff" if "ruff" in name else "mypy",
            "argv": ["?"],
            "timeout_s": 900,
            "diagnostics_argv": None,
        }
        if name.startswith("ignore-"):
            spec = {
                "name": name,
                "parser": "ruff" if name == "ignore-ruff" else "mypy",
                "argv": ["?"],
                "timeout_s": 900,
            }
        root = ctx.run.wt_base if side == "base" else ctx.run.wt_head
        res = gates.collect(spec, side, Path(job["meta"]["out_dir"]), root, job["status"])
        results[f"{name}@{side}"] = res.to_json()
        if side == "base" and not name.startswith("ignore-"):
            key = gates.base_cache_key(spec, head_tree, ps["toolchain"], cfg)
            gates.save_base_cache(cache_dir, key, res)
    for name, key in ps.get("cached", {}).items():
        cached = gates.load_base_cache(cache_dir, key)
        if cached is not None:
            results[f"{name}@base"] = cached.to_json()
    write_json(round_paths(ctx)["gates"], results)
    for root, sha in ((ctx.run.wt_base, ctx.state["merge_base"]), (ctx.run.wt_head, head_commit(ctx))):
        snap.reset_worktree(root, sha)
    unusable = [f"{k}: {v['problem']}" for k, v in results.items() if v["status"] == "unusable"]
    if unusable:
        ctx.escalate(
            "degraded-gate",
            "gate without a usable result: " + "; ".join(unusable)[:2000],
            decision="Fix the gate environment (tools, imports) and rerun.",
        )
    return Step.advance()


def _flaky_candidates(cfg: dict[str, Any], results: dict[str, Any]) -> tuple[list[str], list[str]]:
    """New head failures that also exist at base (rerun on both sides) and brand-new tests."""
    existing: list[str] = []
    new_tests: list[str] = []
    for gate in cfg["repo"]["gates"]:
        if gate["parser"] != "pytest":
            continue
        base = results.get(f"{gate['name']}@base", {}).get("pytest") or {"outcomes": {}, "collected": []}
        head = results.get(f"{gate['name']}@head", {}).get("pytest") or {"outcomes": {}}
        for node, entry in head["outcomes"].items():
            if entry["outcome"] not in ("failed", "error"):
                continue
            base_outcome = base["outcomes"].get(node, {}).get("outcome")
            if base_outcome in ("failed", "error"):
                continue
            (existing if node in base["collected"] else new_tests).append(node)
    return sorted(existing), sorted(new_tests)


@phase("CHECKS")
def checks_phase(ctx: Ctx) -> Step:
    cfg = ctx.config()
    ps = ctx.phase_state()
    results: dict[str, Any] = read_json(round_paths(ctx)["gates"])
    reruns = int(cfg.get("flaky_reruns", 3))
    if "flaky_jobs" not in ps:
        existing, new_tests = _flaky_candidates(cfg, results)
        ps["flaky_nodes"], ps["new_test_failures"] = existing, new_tests
        jobs = []
        pytest_gate = next((g for g in cfg["repo"]["gates"] if g["parser"] == "pytest"), None)
        if existing and reruns and pytest_gate:
            for i in range(1, reruns + 1):
                for side, root in (("base", ctx.run.wt_base), ("head", ctx.run.wt_head)):
                    rerun_gate = {**pytest_gate, "diagnostics_argv": None, "coverage_xml": None}
                    jobs.append(
                        gates.spawn(ctx.run, cfg, rerun_gate, side, root, extra_argv=existing, label=f"-rerun{i}")
                    )
        ps["flaky_jobs"] = jobs
        if jobs:
            return Step.wait(jobs)
    if any(ctx.run.poll_job(j) == "running" for j in ps["flaky_jobs"]):
        return Step.wait([j for j in ps["flaky_jobs"] if ctx.state["jobs"][j]["status"] == "running"])
    flaky: dict[str, str] = {}
    if ps["flaky_jobs"]:
        outcomes: dict[str, dict[str, list[str]]] = {n: {"base": [], "head": []} for n in ps["flaky_nodes"]}
        for job_id in ps["flaky_jobs"]:
            job = ctx.state["jobs"][job_id]
            report_path = Path(job["meta"]["out_dir"]) / "pytest-report.json"
            if not report_path.is_file():
                continue
            report = parsers.parse_pytest_report(report_path.read_text(encoding="utf-8"))
            for node in ps["flaky_nodes"]:
                outcomes[node][job["meta"]["side"]].append(report.outcomes.get(node, {}).get("outcome", "error"))
        for node, sides in outcomes.items():
            flaky[node] = gates.classify_flaky(["failed"] + sides["head"], sides["base"])
        for root, sha in ((ctx.run.wt_base, ctx.state["merge_base"]), (ctx.run.wt_head, head_commit(ctx))):
            snap.reset_worktree(root, sha)

    base_sha, head_sha = ctx.state["merge_base"], head_commit(ctx)
    file_diffs = diffs.diff_between(ctx.repo, base_sha, head_sha)
    classify_anchor = parsers.anchor_change_from_diff(file_diffs)
    severity_map = cfg["repo"].get("severity_map", {})
    mechanical: list[dict[str, Any]] = list(ctx.state["rounds"][str(ctx.round)].get("integrity_findings", []))
    gate_summary: dict[str, Any] = {}
    appendix: dict[str, Any] = {"persisting": [], "suppressed": [], "fixed": [], "changed": [], "flaky": []}
    flaky_excluded = {n for n, c in flaky.items() if c == "flaky"}
    for gate in cfg["repo"]["gates"]:
        base_json, head_json = results.get(f"{gate['name']}@base"), results.get(f"{gate['name']}@head")
        if base_json is None or head_json is None:
            continue
        base_res, head_res = gates.SideResult.from_json(base_json), gates.SideResult.from_json(head_json)
        if "unusable" in (base_res.status, head_res.status):
            gate_summary[gate["name"]] = {"base": base_res.status, "head": head_res.status}
            continue
        diff = parsers.diff_diagnostics(base_res.diagnostics, head_res.diagnostics, classify_anchor)
        limit = gates.error_count_limit(ctx.run.wt_base, gate)
        produced = gates.gate_findings(gate, base_res, head_res, diff, severity_map, ctx.round, limit, flaky_excluded)
        for f in produced:
            node = next(
                (n for n in flaky if f["evidence"]["observed_output"] and n in f["evidence"]["observed_output"]), None
            )
            if node and flaky[node] == "intermittent":
                f["category"] = "concurrency"
                f["title"] = fmod.clamp(f"Intermittent new failure: {node}", 160)
        mechanical.extend(produced)
        extra = gates.ci_failure_without_diagnostics(gate, base_res, head_res, diff, ctx.round)
        if extra:
            mechanical.append(extra)
        gate_summary[gate["name"]] = {
            "base": base_res.status,
            "head": head_res.status,
            **diff.summary(),
            "error_count": {"base": base_res.error_count, "head": head_res.error_count, "limit": limit}
            if gate.get("error_count_gate")
            else None,
        }
        appendix["persisting"] += [d.to_json() for d in diff.persisting[:300]]
        appendix["suppressed"] += [d.to_json() for d in diff.suppressed]
        appendix["fixed"] += [d.to_json() for d in diff.fixed]
        appendix["changed"] += [{"base": b.to_json(), "head": h.to_json()} for b, h in diff.changed]
    appendix["flaky"] = sorted(n for n, c in flaky.items() if c == "flaky")

    mechanical += checks.added_line_suppressions(file_diffs, cfg, ctx.round)
    for name in ("ignore-ruff", "ignore-mypy"):
        res_json = results.get(f"{name}@head")
        if res_json and res_json["status"] != "unusable":
            mechanical += checks.tool_suppression_findings(
                gates.SideResult.from_json(res_json).diagnostics, file_diffs, cfg, ctx.round
            )
    messages = git(ctx.repo, "log", "--format=%B", f"{base_sha}..HEAD", check=False)
    mechanical += checks.changelog_findings(
        ctx.repo, cfg, base_sha, file_diffs, messages, ctx.state.get("intent") or "", ctx.round
    )
    pytest_gate = next((g for g in cfg["repo"]["gates"] if g["parser"] == "pytest"), None)
    head_pytest = results.get(f"{pytest_gate['name']}@head") if pytest_gate else None
    head_report: parsers.PytestReport | None = None
    if head_pytest and head_pytest["status"] != "unusable":
        head_report = gates.SideResult.from_json(head_pytest).pytest
    mechanical += checks.tests_run_findings(cfg, ctx.run.wt_head, file_diffs, head_report, ctx.round)
    coverage_xml = head_pytest.get("coverage_xml") if head_pytest else None
    cov_finding, cov_summary = checks.diff_coverage_finding(cfg, ctx.run.wt_head, file_diffs, coverage_xml, ctx.round)
    if cov_finding:
        mechanical.append(cov_finding)
    mechanical = checks.dedupe(mechanical)
    for f in mechanical:
        problems = fmod.problems(f)
        if problems:
            raise ReviewError(f"internal: invalid mechanical finding {f['id']}: {problems[:3]}")
    rnd = ctx.state["rounds"][str(ctx.round)]
    rnd["gate_summary"] = gate_summary
    rnd["diff_coverage"] = cov_summary
    rnd["flaky"] = flaky
    write_json(round_paths(ctx)["checks"], {"findings": mechanical, "gate_summary": gate_summary, "appendix": appendix})
    store_findings(ctx, mechanical, appendix)
    return Step.advance()
