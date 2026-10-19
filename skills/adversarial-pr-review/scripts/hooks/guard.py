"""PreToolUse guard for review lanes (default deny) and for the author session (protect run state).

Lane mode is active when ADREV_LANE_ID is set (headless lanes) or the hook input's agent_type is
one of this skill's own review agents (`agents/review-*.md`, optionally namespaced as
`<plugin>:<agent>`). Other agents, including a user's own `review-*` subagents, get the author
policy. The lane policy (JSON) comes from ADREV_LANE_POLICY. Every call is appended to the
policy's heartbeat file, so the driver can prove the guard ran.

Exit code 2 (with the reason on stderr) blocks the tool call; so does any internal error.
"""

from __future__ import annotations

import json
import os
import shlex
import sys
import time
from pathlib import Path
from typing import Any

READ_TOOLS = {"Read", "Grep", "Glob", "LS", "NotebookRead"}
WRITE_TOOLS = {"Write"}
EDIT_TOOLS = {"Edit", "MultiEdit", "NotebookEdit"}
SHELL_META = (";", "&", "|", ">", "<", "`", "$(", "${", "\n", "\r")
STATE_FILES = ("state.json", "findings.json", "verdict.json", "ledger.json", "hashes.json")


def _real(path: str, cwd: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path(cwd) / candidate
    # Resolve the parent (the file may not exist yet) and keep the final name, so a symlinked
    # directory can't be used to escape.
    parent = candidate.parent.resolve()
    final = parent / candidate.name
    return final.resolve() if final.exists() or final.is_symlink() else final


def _under(path: Path, roots: list[str]) -> bool:
    for root in roots:
        try:
            path.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _tool_path(tool: str, tool_input: dict[str, Any]) -> str | None:
    for key in ("file_path", "notebook_path", "path"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    if tool == "Glob":
        pattern = tool_input.get("pattern")
        if isinstance(pattern, str) and pattern.startswith("/"):
            return pattern.split("*", 1)[0] or "/"
    return None


def decide_lane(policy: dict[str, Any], event: dict[str, Any]) -> tuple[bool, str]:
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    cwd = str(event.get("cwd") or policy["scratch"])
    if not isinstance(tool_input, dict):
        return False, "malformed tool input"
    if tool in READ_TOOLS:
        target = _tool_path(tool, tool_input)
        path = _real(target, cwd) if target else Path(cwd).resolve()
        if _under(path, policy["read_roots"]):
            return True, "read inside allowed roots"
        return False, f"{tool} outside the review worktrees and scratch dir: {path}"
    if tool in WRITE_TOOLS:
        target = _tool_path(tool, tool_input)
        if not target:
            return False, "Write without a path"
        path = _real(target, cwd)
        if _under(path, [policy["scratch"]]):
            return True, "write inside scratch"
        return False, f"lanes may only write inside their scratch dir, not {path}"
    if tool in EDIT_TOOLS:
        return False, f"{tool} is not available to review lanes"
    if tool == "Bash":
        return _decide_bash(policy, str(tool_input.get("command", "")), cwd)
    return False, f"tool {tool} is not available to review lanes"


def _decide_bash(policy: dict[str, Any], command: str, cwd: str) -> tuple[bool, str]:
    if any(token in command for token in SHELL_META):
        return False, "shell operators are not allowed; run the replay command alone"
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as e:
        return False, f"unparseable command: {e}"
    replay = str(Path(policy["replay"]).resolve())
    if len(argv) < 5 or Path(argv[0]).name not in ("python3", "python") or argv[1:3] != ["-E", "-s"]:
        return False, "only `python3 -E -s <replay.py> lane ...` may run in a lane"
    if str(Path(argv[3]).resolve()) != replay or argv[4] != "lane":
        return False, "only the replay wrapper may run in a lane"
    rest = argv[5:]
    seen: dict[str, str] = {}
    files: list[str] = []
    i = 0
    while i < len(rest):
        token = rest[i]
        if token in ("--run", "--lane", "--side"):
            if i + 1 >= len(rest):
                return False, f"{token} needs a value"
            seen[token] = rest[i + 1]
            i += 2
        elif token in ("--differential",):
            seen[token] = "1"
            i += 1
        elif token.startswith("-"):
            return False, f"unknown option {token}"
        else:
            files.append(token)
            i += 1
    if Path(seen.get("--run", "")).resolve() != Path(policy["run"]).resolve():
        return False, "--run must be this review's run dir"
    if seen.get("--lane") != policy["lane"]:
        return False, "--lane must be this lane"
    if seen.get("--side", "both") not in ("head", "base", "both"):
        return False, "--side must be head, base or both"
    if len(files) != 1:
        return False, "give exactly one repro file"
    if not _under(_real(files[0], cwd), [policy["scratch"]]):
        return False, "the repro file must be inside the scratch dir"
    return True, "replay wrapper"


def shell_hazard(command: str) -> str | None:
    """The first shell construct that could run or redirect something besides one plain command:
    an operator (`; & | < > ( )` or a newline) outside quotes, or a substitution or expansion
    (`` ` ``, `$(`, `${`) outside single quotes. None for a single plain command, whatever text
    its quoted arguments contain."""
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if quote == "'":
            if ch == "'":
                quote = ""
        elif ch == "\\":
            i += 2
            continue
        elif quote == '"':
            if ch == '"':
                quote = ""
            elif ch == "`" or command.startswith(("$(", "${"), i):
                return ch if ch == "`" else command[i : i + 2]
        elif ch in ("'", '"'):
            quote = ch
        elif ch in ";&|<>()\n\r`":
            return ch
        elif command.startswith(("$(", "${"), i):
            return command[i : i + 2]
        i += 1
    return "an unterminated quote" if quote else None


def is_driver_call(command: str, skill_scripts: str) -> bool:
    """A single plain invocation of this skill's own driver, `python3 -E -s <scripts>/review.py ...`."""
    if shell_hazard(command):
        return False
    try:
        argv = shlex.split(command, posix=True)
    except ValueError:
        return False
    if len(argv) < 4 or Path(argv[0]).name not in ("python3", "python") or argv[1:3] != ["-E", "-s"]:
        return False
    try:
        return Path(argv[3]).resolve() == (Path(skill_scripts) / "review.py").resolve()
    except (OSError, RuntimeError):
        return False


def lane_agent_names() -> set[str]:
    agents = Path(__file__).resolve().parent.parent.parent / "agents"
    return {p.stem for p in agents.glob("review-*.md")} if agents.is_dir() else set()


def is_lane_agent(agent_type: str) -> bool:
    """One of this skill's own review agents; plugin agents are namespaced `<plugin>:<agent>`."""
    return bool(agent_type) and agent_type.rsplit(":", 1)[-1] in lane_agent_names()


def decide_author(event: dict[str, Any], skill_scripts: str) -> tuple[bool, str]:
    """Outside lanes, only protect the review's state and worktrees."""
    tool = str(event.get("tool_name", ""))
    tool_input = event.get("tool_input") or {}
    cwd = str(event.get("cwd") or ".")
    if not isinstance(tool_input, dict):
        return True, "not a lane"
    if tool in WRITE_TOOLS | EDIT_TOOLS:
        target = _tool_path(tool, tool_input)
        if target and "/.reviews/" in str(_real(target, cwd)) + "/":
            return False, "the review's run dir is written only by review.py"
        return True, "author edit"
    if tool == "Bash":
        command = str(tool_input.get("command", ""))
        if ".reviews" in command and not is_driver_call(command, skill_scripts):
            writes = any(
                op in command for op in (">", "tee ", "sed -i", "cp ", "mv ", "rm ", "chmod ", "truncate ", "dd ")
            )
            if writes or any(name in command for name in STATE_FILES):
                return False, "the review's run dir is written only by review.py"
    return True, "author command"


def heartbeat(policy: dict[str, Any] | None, event: dict[str, Any], allowed: bool, reason: str) -> None:
    if not policy or not policy.get("heartbeat"):
        return
    record = {"t": round(time.time(), 3), "tool": event.get("tool_name"), "allowed": allowed, "reason": reason[:200]}
    with open(policy["heartbeat"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")


def main() -> int:
    event = json.loads(sys.stdin.read() or "{}")
    lane_env = os.environ.get("ADREV_LANE_ID")
    agent_type = str(event.get("agent_type") or "")
    policy: dict[str, Any] | None = None
    if lane_env or is_lane_agent(agent_type):
        policy_path = os.environ.get("ADREV_LANE_POLICY")
        if not policy_path:
            print("review lane without a policy: denying", file=sys.stderr)
            return 2
        loaded: dict[str, Any] = json.loads(Path(policy_path).read_text(encoding="utf-8"))
        policy = loaded
        allowed, reason = decide_lane(loaded, event)
    else:
        allowed, reason = decide_author(event, str(Path(__file__).resolve().parent.parent))
    heartbeat(policy, event, allowed, reason)
    if allowed:
        return 0
    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(output))
    print(reason, file=sys.stderr)
    return 2


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - any failure blocks the call
        print(f"guard error: {e}", file=sys.stderr)
        sys.exit(2)
