"""A stand-in for `claude -p` in tests: same flags, same hooks, stream-json output, no model.

It runs the PreToolUse and Stop hook commands from `--settings` exactly as Claude Code would
(stdin JSON, exit 2 or a deny decision blocks), applies the dontAsk allowlist from
`--allowedTools`, executes allowed Write and Bash calls, and prints stream-json events.

What it does is chosen per lane by the JSON file in $FAKE_CLAUDE_PLAN, mapping a lane-id
substring to a scenario (first match wins; "*" matches all), or by $FAKE_CLAUDE_SCENARIO:

  ok            write the lane's output (empty findings, or the probe's report) and stop
  findings:F    write the JSON in file F as the output (FAKE_* placeholders are filled in)
  bad-quote:F   like findings:F first with a wrong quote, then fixed after the Stop hook blocks
  crash         exit 1 with no result
  hang          sleep far past any timeout
  invalid-json  write a non-JSON output file
  no-hooks      ignore --settings (as if the settings file failed validation)
  probe-escape  a probe that writes outside its scratch dir (hooks ignored)
  replay:F      write the repro in file F to scratch, run the replay command, then output empty findings

Verification briefs get one ruling per claim: UNSUBSTANTIATED by default, or CONFIRMED / REFUTED
(citing the blamed line, read verbatim from the head tree) with verify-confirm / verify-refute.
Fix verification briefs rule FIXED by default (fixcheck-notfixed, fixcheck-regressed), and
adjudication briefs UNRESOLVED by default (adjudicate-uphold, adjudicate-reject).
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any


def parse_args(argv: list[str]) -> dict[str, Any]:
    opts: dict[str, Any] = {"allowed": [], "add_dir": [], "tools": [], "prompt": None}
    variadic = {"--allowedTools": "allowed", "--add-dir": "add_dir"}
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg in variadic:
            i += 1
            while i < len(argv) and not argv[i].startswith("--"):
                opts[variadic[arg]].append(argv[i])
                i += 1
            continue
        if arg == "-p" and i + 1 < len(argv) and not argv[i + 1].startswith("-"):
            opts["prompt"] = argv[i + 1]
            i += 1
        elif arg in ("-p", "--restricted", "--strict-mcp-config", "--no-session-persistence", "--verbose"):
            opts[arg.lstrip("-")] = True
        elif arg.startswith("--") and i + 1 < len(argv):
            opts[arg[2:]] = argv[i + 1]
            i += 1
        i += 1
    opts["tools"] = [t for t in str(opts.get("tools", "")).split(",") if t]
    return opts


class Session:
    def __init__(self, opts: dict[str, Any], scenario: str) -> None:
        self.opts = opts
        self.scenario = scenario
        self.hooks_on = scenario not in ("no-hooks", "probe-escape")
        settings = json.loads(Path(opts["settings"]).read_text()) if opts.get("settings") and self.hooks_on else {}
        self.hooks = settings.get("hooks", {})
        self.denials: list[dict[str, Any]] = []
        self.turns = 0
        self.cwd = os.getcwd()

    def emit(self, event: dict[str, Any]) -> None:
        print(json.dumps(event), flush=True)

    def run_hooks(self, name: str, event: dict[str, Any]) -> tuple[bool, str]:
        for group in self.hooks.get(name, []):
            if name == "PreToolUse" and not fnmatch.fnmatch(event.get("tool_name", ""), group.get("matcher", "*")):
                continue
            for hook in group.get("hooks", []):
                proc = subprocess.run(
                    hook["command"], shell=True, input=json.dumps(event), capture_output=True, text=True, check=False
                )
                out = proc.stdout.strip()
                decision = None
                if out.startswith("{"):
                    try:
                        decision = json.loads(out)
                    except ValueError:
                        decision = None
                if proc.returncode == 2:
                    return False, proc.stderr.strip()
                if decision and decision.get("decision") == "block":
                    return False, str(decision.get("reason"))
                spec = (decision or {}).get("hookSpecificOutput") or {}
                if spec.get("permissionDecision") == "deny":
                    return False, str(spec.get("permissionDecisionReason"))
        return True, ""

    def allowlisted(self, tool: str, tool_input: dict[str, Any]) -> bool:
        if tool in ("Read", "Grep", "Glob"):
            return True
        for rule in self.opts["allowed"]:
            if tool == "Write" and rule.startswith("Edit(//") and rule.endswith("/**)"):
                root = rule[len("Edit(/") : -len("/**)")]
                if str(Path(tool_input.get("file_path", "")).resolve()).startswith(root.rstrip("/") + "/"):
                    return True
            if tool == "Bash" and rule.startswith("Bash(") and rule.endswith(" *)"):
                prefix = rule[len("Bash(") : -len(" *)")]
                if str(tool_input.get("command", "")).startswith(prefix + " "):
                    return True
        return False

    def call(self, tool: str, tool_input: dict[str, Any]) -> str:
        self.turns += 1
        use_id = f"toolu_fake_{self.turns}"
        self.emit(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": use_id, "name": tool, "input": tool_input}],
                },
            }
        )
        event = {
            "session_id": "fake",
            "cwd": self.cwd,
            "hook_event_name": "PreToolUse",
            "tool_name": tool,
            "tool_input": tool_input,
        }
        allowed, reason = self.run_hooks("PreToolUse", event) if self.hooks_on else (True, "")
        if allowed and self.hooks_on and not self.allowlisted(tool, tool_input):
            allowed, reason = False, "not allowed in dontAsk mode"
        if not allowed:
            self.denials.append({"tool_name": tool, "tool_use_id": use_id, "tool_input": tool_input})
            result = f"denied: {reason}"
        elif tool == "Write":
            path = Path(tool_input["file_path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(tool_input["content"], encoding="utf-8")
            result = "written"
        elif tool == "Bash":
            proc = subprocess.run(shlex.split(tool_input["command"]), capture_output=True, text=True, check=False)
            result = proc.stdout + proc.stderr
        else:
            result = "ok"
        self.emit(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": use_id, "content": result}],
                },
            }
        )
        return result

    def stop(self, final: str, fix: Any = None) -> bool:
        """Run the Stop hook; on a block, call ``fix`` once per retry. True if the stop was accepted."""
        active = False
        for _ in range(4):
            event = {
                "session_id": "fake",
                "cwd": self.cwd,
                "hook_event_name": "Stop",
                "stop_hook_active": active,
                "last_assistant_message": final,
            }
            accepted, _reason = self.run_hooks("Stop", event) if self.hooks_on else (True, "")
            if accepted:
                return True
            active = True
            if fix is not None:
                fix()
        return False


def fill(text: str, values: dict[str, str]) -> str:
    for key, value in values.items():
        text = text.replace(key, value)
    return text


def outer_agent(opts: dict[str, Any]) -> int:
    """Play the benchmark's outer session for `/adversarial-review-v2 ...`: follow SKILL.md's protocol
    (start, then run each exec/wait action) under the dontAsk allowlist, and emit stream-json."""
    prompt = str(opts["prompt"])
    user_level = Path(os.environ.get("CLAUDE_CONFIG_DIR", "/nonexistent")) / "skills" / "adversarial-review-v2"
    skill = user_level if user_level.is_dir() else Path.cwd() / ".claude" / "skills" / "adversarial-review-v2"
    s = Session({**opts, "settings": None}, "outer")
    s.hooks_on = False
    s.emit({"type": "system", "subtype": "init", "permissionMode": opts.get("permission-mode"), "tools": ["Bash", "Read"],
            "skills": sorted(p.name for p in (skill.parent.iterdir() if skill.parent.is_dir() else [])),
            "mcp_servers": [], "model": "fake", "claude_code_version": "fake"})  # fmt: skip
    rest = prompt.split(None, 1)[1] if " " in prompt else ""
    no_fix = rest.startswith("--no-fix")
    intent = rest.removeprefix("--no-fix").strip()
    command = shlex.join(
        [
            "python3",
            "-E",
            "-s",
            str(skill / "scripts" / "review.py"),
            "start",
            *(["--no-fix"] if no_fix else []),
            intent,
        ]
    )
    verdict = "no verdict"
    for _ in range(200):
        allowed = any(
            command.startswith(r[len("Bash(") : -len(" *)")] + " ") for r in opts["allowed"] if r.startswith("Bash(")
        )
        s.turns += 1
        use_id = f"toolu_outer_{s.turns}"
        s.emit(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "id": use_id, "name": "Bash", "input": {"command": command}}],
                },
            }
        )
        if not allowed:
            s.denials.append({"tool_name": "Bash", "tool_use_id": use_id, "tool_input": {"command": command}})
            break
        proc = subprocess.run(shlex.split(command), capture_output=True, text=True, check=False)
        s.emit(
            {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [{"type": "tool_result", "tool_use_id": use_id, "content": proc.stdout[-2000:]}],
                },
            }
        )
        try:
            action = json.loads(proc.stdout)["action"]
        except (ValueError, KeyError):
            break
        if action["kind"] in ("done", "escalate"):
            verdict = action["verdict"]
            break
        if action["kind"] not in ("exec", "wait"):
            break
        command = action["command"]
    s.emit({"type": "result", "subtype": "success", "is_error": False, "result": f"Verdict: {verdict}", "total_cost_usd": 0.05,
            "num_turns": s.turns, "usage": {}, "modelUsage": {}, "permission_denials": s.denials})  # fmt: skip
    return 0


def main() -> int:
    if sys.argv[1:] == ["--version"]:
        print(f"{os.environ.get('FAKE_CLAUDE_VERSION', '2.1.303')} (Claude Code)")
        return 0
    if sys.argv[1:3] == ["auth", "status"]:
        logged_in = os.environ.get("FAKE_CLAUDE_LOGGED_IN", "1") == "1"
        print(json.dumps({"loggedIn": logged_in, "authMethod": "fake", "email": "someone@example.invalid"}))
        return 0 if logged_in else 1
    opts = parse_args(sys.argv[1:])
    if opts.get("prompt") and str(opts["prompt"]).startswith("/adversarial-review-v2"):
        return outer_agent(opts)
    brief = sys.stdin.read()
    lane = os.environ.get("ADREV_LANE_ID", "")
    scenario = os.environ.get("FAKE_CLAUDE_SCENARIO", "ok")
    plan_file = os.environ.get("FAKE_CLAUDE_PLAN")
    if plan_file and Path(plan_file).is_file():
        for pattern, chosen in json.loads(Path(plan_file).read_text()).items():
            if pattern == "*" or pattern in lane:
                scenario = chosen
                break
    if scenario == "crash":
        print("fake claude: crashing", file=sys.stderr)
        return 1
    if scenario == "hang":
        time.sleep(3600)
        return 1
    s = Session(opts, scenario)
    s.emit(
        {
            "type": "system",
            "subtype": "init",
            "permissionMode": opts.get("permission-mode"),
            "tools": opts["tools"],
            "mcp_servers": [],
            "model": opts.get("model"),
            "cwd": s.cwd,
            "claude_code_version": "fake",
        }
    )
    out_match = re.search(r"^DONE (\S+)$", brief, re.M)
    out_path = Path(out_match.group(1)) if out_match else Path(s.cwd) / "out.json"
    final = f"DONE {out_path}"
    roots = {
        "FAKE_HEAD": opts["add_dir"][0] if opts["add_dir"] else "",
        "FAKE_BASE": opts["add_dir"][1] if len(opts["add_dir"]) > 1 else "",
    }
    if brief.startswith("# Verification brief"):
        verdict = {"verify-confirm": "CONFIRMED", "verify-refute": "REFUTED"}.get(scenario, "UNSUBSTANTIATED")
        head_root = Path(opts["add_dir"][0]) if opts["add_dir"] else Path(s.cwd)
        rulings = []
        for ref, side, file, line in re.findall(
            r"^### (C\d+):.*?^- Changed lines it blames: (head|base) `([^`]+)`:(\d+)", brief, re.M | re.S
        ):
            text = (head_root / file).read_text().splitlines()[int(line) - 1].strip() if side == "head" else ""
            cite = (
                [{"side": side, "file": file, "line": int(line), "quote": text}] if verdict != "UNSUBSTANTIATED" else []
            )
            rulings.append(
                {
                    "finding_ref": ref,
                    "verdict": verdict,
                    "evidence_kind": {"CONFIRMED": "citation_chain", "REFUTED": "file_line"}.get(verdict, "none"),
                    "citations": cite,
                    "counter_test": None,
                    "expectation_source": "intent",
                    "severity_assessment": "agree",
                    "reason": "fake ruling",
                }
            )
        s.call("Write", {"file_path": str(out_path), "content": json.dumps({"rulings": rulings})})
        s.stop(final)
    elif brief.startswith("# Fix verification brief"):
        verdict = {"fixcheck-notfixed": "NOT_FIXED", "fixcheck-regressed": "REGRESSED"}.get(scenario, "FIXED")
        head_root = Path(opts["add_dir"][0])
        rulings = []
        for ref, file, line in re.findall(
            r"^### (C\d+):.*?^- Location: head `([^`]+)` lines (\d+)-", brief, re.M | re.S
        ):
            lines = (head_root / file).read_text().splitlines()
            n = min(int(line), len(lines))
            rulings.append(
                {
                    "finding_ref": ref,
                    "verdict": verdict,
                    "citations": [{"side": "head", "file": file, "line": n, "quote": lines[n - 1].strip()}],
                    "reason": "fake fix ruling",
                }
            )
        s.call("Write", {"file_path": str(out_path), "content": json.dumps({"rulings": rulings})})
        s.stop(final)
    elif brief.startswith("# Adjudication brief"):
        ruling = {"adjudicate-uphold": "UPHOLD", "adjudicate-reject": "REJECT"}.get(scenario, "UNRESOLVED")
        head_root = Path(opts["add_dir"][0])
        blamed = re.search(r"^- Changed lines it blames: head `([^`]+)`:(\d+)", brief, re.M)
        cites = []
        if blamed and ruling != "UNRESOLVED":
            lines = (head_root / blamed.group(1)).read_text().splitlines()
            n = min(int(blamed.group(2)), len(lines))
            cites = [{"side": "head", "file": blamed.group(1), "line": n, "quote": lines[n - 1].strip()}]
        kind = {"UPHOLD": "citation_chain", "REJECT": "file_line"}.get(ruling, "none")
        payload = {"ruling": ruling, "evidence_kind": kind, "citations": cites, "reason": "fake adjudication"}
        s.call("Write", {"file_path": str(out_path), "content": json.dumps(payload)})
        s.stop(final)
    elif "Safety probe" in brief:
        write_target = re.search(r"create the file `([^`]+)` with the content `probe`", brief)
        touch = re.search(r"run exactly this command: `([^`]+)`", brief)
        nonce = re.search(r"Safety probe (\S+)\.", brief)
        if scenario == "probe-escape" and write_target:
            Path(write_target.group(1)).write_text("probe")
        if write_target:
            s.call("Write", {"file_path": write_target.group(1), "content": "probe"})
        if touch:
            s.call("Bash", {"command": touch.group(1)})
        report = {
            "probe": nonce.group(1) if nonce else "?",
            "attempts": [{"step": 1, "result": "refused"}, {"step": 2, "result": "refused"}],
        }
        s.call("Write", {"file_path": str(out_path), "content": json.dumps(report)})
        s.stop(final)
    else:
        kind, _, arg = scenario.partition(":")
        if kind == "replay":
            repro = Path(arg)
            target = Path(s.cwd) / repro.name
            s.call("Write", {"file_path": str(target), "content": repro.read_text()})
            command = re.search(r"^python3 -E -s (\S+) lane --run (\S+) --lane (\S+) --side both \S+$", brief, re.M)
            if command:
                s.call(
                    "Bash",
                    {
                        "command": f"python3 -E -s {command.group(1)} lane --run {command.group(2)} --lane {command.group(3)} --side both {target}"
                    },
                )
        if kind in ("findings", "bad-quote"):
            good = fill(Path(arg).read_text(), roots)
            first = good
            if kind == "bad-quote":
                data = json.loads(good)
                for f in data["findings"]:
                    if f["location"]["quote"]:
                        f["location"]["quote"] = "this line does not exist anywhere"
                first = json.dumps(data)
            s.call("Write", {"file_path": str(out_path), "content": first})
            s.stop(final, fix=lambda: s.call("Write", {"file_path": str(out_path), "content": good}))
        elif kind == "invalid-json":
            s.call("Write", {"file_path": str(out_path), "content": "{not json"})
            s.stop(final)
        else:
            s.call("Write", {"file_path": str(out_path), "content": json.dumps({"findings": [], "notes": None})})
            s.stop(final)
    s.emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": final,
            "total_cost_usd": 0.01,
            "num_turns": s.turns,
            "usage": {
                "input_tokens": 100,
                "output_tokens": 50,
                "cache_read_input_tokens": 1000,
                "cache_creation_input_tokens": 200,
            },
            "modelUsage": {},
            "permission_denials": s.denials,
        }
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
