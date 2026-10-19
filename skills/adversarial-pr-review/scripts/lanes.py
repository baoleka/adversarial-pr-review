"""Headless review lanes: one `claude -p` process per lane, fully specified by the driver.

Each lane gets:
- a scratch dir `<run>/agents/<id>/` (its cwd and the only place it may write), and
- a control dir `<run>/lanes/<id>/` it can't read or write: the brief, the system text, the
  per-lane settings (guard and output-validator hooks), the guard policy, the guard heartbeat and
  the validator's retry counter.

Layers that confine a lane: `--restricted` (no user/project settings, file tools confined to the
working dirs), `--permission-mode dontAsk` with an explicit allowlist, the PreToolUse guard
(default deny; Bash only for the replay wrapper), and read-only worktrees. Every lane's result is
checked against its init event and the guard heartbeat, so a lane whose settings were silently
ignored fails closed.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from common import SCHEMAS_DIR, SCRIPTS_DIR, canonical_json, read_json, sha256_text, write_json
from jsonschema_lite import validate
from state import Run

LANE_TOOLS = ("Read", "Grep", "Glob", "Write", "Bash")
SCHEMA_FOR = {
    "findings": "finding.wire.schema.json",
    "verifier": "verifier-output.schema.json",
    "fixcheck": "fixcheck-output.schema.json",
    "adjudicator": "adjudicator-output.schema.json",
    "probe": "probe-output.schema.json",
}
MAX_VALIDATOR_RETRIES = 2
# Variables of the outer session that must not leak into a lane.
UNSET_ENV = (
    "CLAUDE_CODE_ADDITIONAL_DIRECTORIES_CLAUDE_MD",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_SSE_PORT",
    "ENABLE_IDE_INTEGRATION",
    "CLAUDE_CODE_SIMPLE",
    "ADREV_LANE_ID",
    "ADREV_LANE_POLICY",
)


@dataclass
class LaneSpec:
    id: str
    kind: str  # breaker | external | contracts | tests | security | interface | delta | verifier | ... | probe
    agent: str  # agents/<agent>.md
    model: str
    effort: str
    max_turns: int
    timeout_s: int
    lens: str
    unit: str | None
    output_kind: str
    round: int
    estimate_usd: float
    budget_cap_usd: float = 0.0
    files: list[str] = field(default_factory=list)
    attempt: int = 1

    def to_json(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> LaneSpec:
        return LaneSpec(**dict(data))


@dataclass
class LaneResult:
    status: str  # ok | failed | timeout | invalid
    problem: str | None
    cost_usd: float
    cost_measured: bool
    usage: dict[str, Any]
    permission_denials: list[dict[str, Any]]
    tool_uses: int
    guard_calls: int
    stop_checks: int
    integrity: list[str]
    output: Any = None
    init: dict[str, Any] | None = None

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data.pop("output", None)
        return data


# ---------------------------------------------------------------- paths


def scratch_dir(run: Run, lane_id: str) -> Path:
    return run.agent_dir(lane_id)


def control_dir(run: Run, lane_id: str) -> Path:
    path = run.dir / "lanes" / lane_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def out_path(run: Run, lane_id: str) -> Path:
    return scratch_dir(run, lane_id) / "out.json"


def replay_script() -> Path:
    return SCRIPTS_DIR / "replay.py"


def replay_command(run: Run, lane_id: str, example: str = "test_repro.py") -> str:
    scratch = scratch_dir(run, lane_id)
    return f"python3 -E -s {replay_script()} lane --run {run.dir} --lane {lane_id} --side both {scratch / example}"


# ---------------------------------------------------------------- control files


def policy_for(run: Run, spec: LaneSpec) -> dict[str, Any]:
    scratch = scratch_dir(run, spec.id)
    control = control_dir(run, spec.id)
    return {
        "lane": spec.id,
        "run": str(run.dir),
        "scratch": str(scratch),
        "read_roots": [str(run.wt_head), str(run.wt_base), str(scratch)],
        "replay": str(replay_script()),
        "heartbeat": str(control / "guard.log"),
        "out_path": str(scratch / "out.json"),
        "schema": str(SCHEMAS_DIR / SCHEMA_FOR[spec.output_kind]),
        "output_kind": spec.output_kind,
        "quote_roots": {"head": str(run.wt_head), "base": str(run.wt_base)},
        "counter_file": str(control / "validator-count"),
        "max_retries": MAX_VALIDATOR_RETRIES,
    }


def hook_command(script: str) -> str:
    return f"{shlex.quote(sys.executable)} -E -s {shlex.quote(str(SCRIPTS_DIR / 'hooks' / script))}"


def settings_for() -> dict[str, Any]:
    """Per-lane settings: only hooks (a settings file that fails validation is silently ignored in
    -p mode, so it is kept minimal; the heartbeat check catches it if it is ignored anyway)."""
    return {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "*",
                    "hooks": [
                        {"type": "command", "command": hook_command("guard.py"), "timeout": 10, "onFailure": "block"}
                    ],
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": hook_command("validate_output.py"), "timeout": 120}]}],
        }
    }


def allowed_tools(run: Run, spec: LaneSpec) -> list[str]:
    scratch = scratch_dir(run, spec.id)
    return [
        "Read",
        "Grep",
        "Glob",
        f"Edit(/{scratch}/**)",
        f"Bash(python3 -E -s {replay_script()} lane *)",
    ]


def lane_argv(run: Run, spec: LaneSpec, claude_bin: str) -> list[str]:
    control = control_dir(run, spec.id)
    return [
        claude_bin,
        "-p",
        "--model",
        spec.model,
        "--effort",
        spec.effort,
        "--restricted",
        "--tools",
        ",".join(LANE_TOOLS),
        "--permission-mode",
        "dontAsk",
        "--permission-prompts",
        "none",
        "--allowedTools",
        *allowed_tools(run, spec),
        "--settings",
        str(control / "settings.json"),
        "--strict-mcp-config",
        "--mcp-config",
        str(control / "mcp.json"),
        "--add-dir",
        str(run.wt_head),
        "--add-dir",
        str(run.wt_base),
        "--append-system-prompt-file",
        str(control / "system.md"),
        "--max-turns",
        str(spec.max_turns),
        "--max-budget-usd",
        f"{max(spec.budget_cap_usd, 0.01):.2f}",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
    ]


def lane_env(run: Run, spec: LaneSpec) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in UNSET_ENV}
    env["CLAUDE_CODE_DISABLE_CLAUDE_MDS"] = "1"
    env["ADREV_LANE_ID"] = spec.id
    env["ADREV_LANE_POLICY"] = str(control_dir(run, spec.id) / "policy.json")
    env["ADREV_RUN"] = str(run.dir)
    return env


def prepare(run: Run, spec: LaneSpec, brief: str, system: str) -> dict[str, str]:
    """Write the lane's control files; return their hashes (recorded in the run state)."""
    control = control_dir(run, spec.id)
    scratch = scratch_dir(run, spec.id)
    for leftover in (scratch / "out.json", control / "guard.log", control / "validator-count"):
        if leftover.exists():
            leftover.unlink()
    (control / "brief.md").write_text(brief, encoding="utf-8")
    (control / "system.md").write_text(system, encoding="utf-8")
    write_json(control / "policy.json", policy_for(run, spec))
    write_json(control / "settings.json", settings_for())
    write_json(control / "mcp.json", {"mcpServers": {}})
    write_json(control / "spec.json", spec.to_json())
    return {
        "brief": sha256_text(brief),
        "system": sha256_text(system),
        "settings": sha256_text(canonical_json(settings_for())),
    }


def spawn(run: Run, spec: LaneSpec, claude_bin: str) -> str:
    control = control_dir(run, spec.id)
    return run.spawn_job(
        "lane",
        lane_argv(run, spec, claude_bin),
        scratch_dir(run, spec.id),
        lane_env(run, spec),
        spec.timeout_s,
        meta={"lane": spec.id, "kind": spec.kind},
        stdin_file=control / "brief.md",
    )


# ---------------------------------------------------------------- collecting


def _rejected_before_running(block: dict[str, Any]) -> bool:
    """A tool_result Claude Code produced without running the tool (unknown tool, invalid input):
    no hook runs for such a call, and it executes nothing."""
    content = block.get("content")
    if isinstance(content, list):
        content = " ".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
    return bool(block.get("is_error")) and str(content).lstrip().startswith("<tool_use_error>")


def parse_stream(text: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None, int]:
    """(init event, result event, number of tool calls that reached execution) from stream-json.

    Every executed call passes the PreToolUse guard, so the guard heartbeat must count at least as
    many; calls rejected before running don't reach hooks and aren't counted."""
    init = result = None
    tool_uses = 0
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        kind = event.get("type")
        if kind == "system" and event.get("subtype") == "init":
            init = event
        elif kind == "assistant":
            content = (event.get("message") or {}).get("content") or []
            tool_uses += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_use")
        elif kind == "user":
            content = (event.get("message") or {}).get("content") or []
            if isinstance(content, list):
                tool_uses -= sum(
                    1
                    for block in content
                    if isinstance(block, dict)
                    and block.get("type") == "tool_result"
                    and _rejected_before_running(block)
                )
        elif kind == "result":
            result = event
    return init, result, tool_uses


def read_heartbeat(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records


def init_problems(init: Mapping[str, Any] | None) -> list[str]:
    """The lane must have started with exactly the configuration the driver asked for."""
    if init is None:
        return ["no init event: the lane's configuration can't be checked"]
    problems = []
    mode = init.get("permissionMode")
    if mode != "dontAsk":
        problems.append(f"permission mode is {mode!r}, not 'dontAsk'")
    tools = init.get("tools")
    if not isinstance(tools, list):
        problems.append("init event has no tool list")
    else:
        extra = sorted(set(map(str, tools)) - set(LANE_TOOLS))
        if extra:
            problems.append(f"unexpected tools available: {', '.join(extra)[:300]}")
    servers = init.get("mcp_servers")
    if servers:
        problems.append(f"MCP servers loaded: {json.dumps(servers)[:300]}")
    return problems


def collect(run: Run, spec: LaneSpec, job_id: str) -> LaneResult:
    job = run.state["jobs"][job_id]
    control = control_dir(run, spec.id)
    stdout = run.job_output(job_id, "stdout")
    init, result, tool_uses = parse_stream(stdout)
    beats = read_heartbeat(control / "guard.log")
    guard_calls = sum(1 for b in beats if b.get("tool") != "Stop")
    stop_checks = sum(1 for b in beats if b.get("tool") == "Stop")
    integrity = init_problems(init) if stdout.strip() else []
    if tool_uses > guard_calls:
        integrity.append(f"the guard saw {guard_calls} of {tool_uses} tool calls: lane hooks did not run")
    usage: dict[str, Any] = {}
    denials: list[dict[str, Any]] = []
    cost, measured = spec.estimate_usd, False
    if result is not None:
        from telemetry import usage_from_result

        usage = usage_from_result(result)
        cost, measured = float(usage["cost_usd"]), True
        denials = [d for d in result.get("permission_denials") or [] if isinstance(d, dict)]
    status, problem, output = "ok", None, None
    if job["status"] == "timeout":
        status, problem = "timeout", f"lane timed out after {spec.timeout_s}s"
    elif result is None:
        tail = run.job_output(job_id, "stderr")[-500:]
        status, problem = "failed", f"lane exited without a result (exit {job.get('exit_code')}): {tail}"
    elif result.get("is_error") or result.get("subtype") != "success":
        status, problem = "failed", f"lane ended with {result.get('subtype')}: {str(result.get('result'))[:300]}"
    if status == "ok":
        output, problem = load_output(run, spec)
        if problem:
            status = "invalid"
    if integrity:
        status = "failed"
        problem = "integrity: " + "; ".join(integrity)
    return LaneResult(
        status=status,
        problem=problem,
        cost_usd=round(cost, 6),
        cost_measured=measured,
        usage=usage,
        permission_denials=denials,
        tool_uses=tool_uses,
        guard_calls=guard_calls,
        stop_checks=stop_checks,
        integrity=integrity,
        output=output,
        init={k: init.get(k) for k in ("model", "permissionMode", "tools", "mcp_servers", "cwd", "claude_code_version")}
        if init
        else None,
    )


def load_output(run: Run, spec: LaneSpec) -> tuple[Any, str | None]:
    path = out_path(run, spec.id)
    if not path.is_file():
        return None, f"no output at {path}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except ValueError as e:
        return None, f"output is not JSON: {e}"
    schema = read_json(SCHEMAS_DIR / SCHEMA_FOR[spec.output_kind])
    problems = validate(payload, schema)
    if problems:
        return None, "output does not match the schema: " + "; ".join(problems[:5])
    return payload, None
