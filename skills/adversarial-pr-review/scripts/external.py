"""External (non-Claude) lane plumbing for the driver: export, spawn `run_external.py`, collect.

Only used when `external.provider` is "codex". The lane reads only an export of the snapshot
(denylisted files left out), and `run_external.py` refuses to call Codex unless a read-confinement
canary proves Codex can't read outside that export. Any refusal or failure falls back to the
Claude second lens, and the run records `single_family: true`.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

import export as export_mod
import telemetry
from lanes import LaneResult, LaneSpec, control_dir, load_output, out_path

from common import SCHEMAS_DIR, SCRIPTS_DIR, write_json
from state import Run


def codex_problem(cfg: dict[str, Any]) -> str | None:
    if cfg["external"]["provider"] != "codex":
        return "external lanes are disabled (external.provider is none)"
    if not shutil.which(str(cfg["external"].get("codex_bin", "codex"))):
        return "the codex CLI is not installed"
    return None


def prepare(
    run: Run, spec: LaneSpec, brief: str, cfg: dict[str, Any], commits: dict[str, str], repo: Path
) -> dict[str, Any]:
    control = control_dir(run, spec.id)
    export_root = control / "export"
    if export_root.exists():
        shutil.rmtree(export_root)
    skipped = {}
    for side, commit in commits.items():
        skipped[side] = export_mod.export(
            repo, commit, export_root / side, cfg["external"]["allow_globs"], cfg["external"]["deny_globs"]
        )
    (control / "codex-brief.md").write_text(brief, encoding="utf-8")
    spec_json = {
        "codex_bin": str(cfg["external"].get("codex_bin", "codex")),
        "model": cfg["external"].get("model"),
        "export_dir": str(export_root),
        "schema": str(SCHEMAS_DIR / "finding.wire.schema.json"),
        "brief": str(control / "codex-brief.md"),
        "out": str(out_path(run, spec.id)),
        "timeout_s": int(cfg["external"]["timeout_s"]),
    }
    write_json(control / "external.json", spec_json)
    return {"export_root": str(export_root), "skipped": skipped}


def spawn(run: Run, spec: LaneSpec) -> str:
    control = control_dir(run, spec.id)
    argv = [sys.executable, "-E", "-s", str(SCRIPTS_DIR / "run_external.py"), str(control)]
    import os

    env = {k: v for k, v in os.environ.items() if not k.startswith("ADREV_")}
    return run.spawn_job(
        "external", argv, control, env, spec.timeout_s + 360, meta={"lane": spec.id, "kind": "external"}
    )


def collect(run: Run, spec: LaneSpec, job_id: str, cfg: dict[str, Any]) -> LaneResult:
    control = control_dir(run, spec.id)
    status_file = control / "status.json"
    status: dict[str, Any] = json.loads(status_file.read_text(encoding="utf-8")) if status_file.is_file() else {}
    usage = status.get("usage") or {}
    price = telemetry.price(cfg, str(cfg["external"].get("model") or "unknown"))
    uncached = max(0, int(usage.get("input_tokens", 0)) - int(usage.get("cached_input_tokens", 0)))
    cost = (
        uncached * price["input"]
        + int(usage.get("cached_input_tokens", 0)) * price["cache_read"]
        + (int(usage.get("output_tokens", 0)) + int(usage.get("reasoning_output_tokens", 0))) * price["output"]
    ) / 1_000_000
    state = status.get("status") or ("timeout" if run.state["jobs"][job_id]["status"] == "timeout" else "failed")
    problem = None if state == "ok" else f"codex lane {state}: {status.get('canary', '')}"
    output = None
    if state == "ok":
        output, problem = load_output(run, spec)
        if problem:
            state = "invalid"
    lane_status = {"ok": "ok", "invalid": "invalid", "timeout": "timeout"}.get(state, "failed")
    tokens = {
        "input": uncached,
        "output": int(usage.get("output_tokens", 0)) + int(usage.get("reasoning_output_tokens", 0)),
        "cache_read": int(usage.get("cached_input_tokens", 0)),
        "cache_write": 0,
        "turns": 0,
    }
    return LaneResult(
        status=lane_status,
        problem=problem,
        cost_usd=round(cost, 6),
        cost_measured=bool(usage),
        usage=tokens,
        permission_denials=[],
        tool_uses=0,
        guard_calls=0,
        stop_checks=0,
        integrity=[],
        output=output,
        init={"runner": "codex", "canary": status.get("canary"), "refused": state == "refused"},
    )
