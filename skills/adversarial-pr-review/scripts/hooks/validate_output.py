"""Stop hook for review lanes: accept the turn's end only with valid output.

The lane must write its output to the path the driver registered (never one it chooses) and end
with `DONE <path>`. The output must match the lane's schema, and every quote must be verbatim. On a
problem the hook blocks the stop with the list of errors, at most ``max_retries`` times (counted
in a file outside the lane's scratch dir); after that it lets the lane stop and the driver, which
re-validates everything, marks the lane invalid.
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.dont_write_bytecode = True  # don't write __pycache__ into the install dir
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import quotes  # noqa: E402

from jsonschema_lite import validate  # noqa: E402


def last_message(event: dict[str, Any]) -> str | None:
    message = event.get("last_assistant_message")
    if isinstance(message, str):
        return message
    transcript = event.get("transcript_path")
    if not isinstance(transcript, str) or not Path(transcript).is_file():
        return None
    text = None
    for line in Path(transcript).read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        msg = record.get("message") if isinstance(record, dict) else None
        if isinstance(msg, dict) and msg.get("role") == "assistant":
            parts = msg.get("content")
            if isinstance(parts, list):
                texts = [p.get("text", "") for p in parts if isinstance(p, dict) and p.get("type") == "text"]
                if texts:
                    text = "\n".join(texts)
    return text


def quote_problems(payload: dict[str, Any], roots: dict[str, str]) -> list[str]:
    problems = []
    for index, finding in enumerate(payload.get("findings") or []):
        loc = finding.get("location") or {}
        if finding.get("kind") == "defect":
            if not loc.get("quote"):
                problems.append(f"findings[{index}]: a defect needs location.quote copied verbatim from the file")
            else:
                check = quotes.check(Path(roots[loc["side"]]), loc["file"], int(loc["start_line"]), loc["quote"])
                if not check.ok:
                    problems.append(f"findings[{index}].location.quote: {check.reason}")
        if int(loc.get("end_line", 1)) < int(loc.get("start_line", 1)):
            problems.append(f"findings[{index}].location: end_line is before start_line")
        chain = (finding.get("evidence") or {}).get("citation_chain") or []
        for hop, cite in enumerate(chain):
            check = quotes.check(Path(roots[cite["side"]]), cite["file"], int(cite["line"]), cite["quote"])
            if not check.ok:
                problems.append(f"findings[{index}].evidence.citation_chain[{hop}]: {check.reason}")
    return problems


def citation_problems(payload: dict[str, Any], roots: dict[str, str], key: str) -> list[str]:
    problems = []
    rulings = payload.get(key) if key else [payload]
    for index, ruling in enumerate(rulings or []):
        for hop, cite in enumerate(ruling.get("citations") or []):
            check = quotes.check(Path(roots[cite["side"]]), cite["file"], int(cite["line"]), cite["quote"])
            if not check.ok:
                problems.append(f"{key or 'output'}[{index}].citations[{hop}]: {check.reason}")
    return problems


def problems_for(policy: dict[str, Any], message: str | None) -> list[str]:
    out_path = Path(policy["out_path"])
    problems = []
    if message is not None:
        lines = [line.strip() for line in message.strip().splitlines() if line.strip()]
        if not lines or lines[-1] != f"DONE {out_path}":
            problems.append(f"end your turn with exactly one final line: DONE {out_path}")
    if not out_path.is_file():
        return problems + [f"write your output to {out_path} with the Write tool"]
    try:
        payload = json.loads(out_path.read_text(encoding="utf-8"))
    except ValueError as e:
        return problems + [f"{out_path} is not valid JSON: {e}"]
    schema = json.loads(Path(policy["schema"]).read_text(encoding="utf-8"))
    schema_problems = validate(payload, schema)
    if schema_problems:
        return problems + [f"schema: {p}" for p in schema_problems[:20]]
    roots = policy.get("quote_roots") or {}
    kind = policy.get("output_kind")
    if kind == "findings":
        problems += quote_problems(payload, roots)
    elif kind in ("verifier", "fixcheck"):
        problems += citation_problems(payload, roots, "rulings")
    elif kind == "adjudicator":
        problems += citation_problems(payload, roots, "")
    return problems


def main() -> int:
    event = json.loads(sys.stdin.read() or "{}")
    policy_path = os.environ.get("ADREV_LANE_POLICY")
    if not policy_path:
        return 0
    policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
    problems = problems_for(policy, last_message(event))
    counter = Path(policy["counter_file"])
    used = int(counter.read_text(encoding="utf-8").strip() or 0) if counter.is_file() else 0
    record = {"t": round(time.time(), 3), "tool": "Stop", "problems": problems[:20], "retries_used": used}
    with open(policy["heartbeat"], "a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, sort_keys=True) + "\n")
    if not problems or used >= int(policy.get("max_retries", 2)):
        return 0
    counter.write_text(str(used + 1), encoding="utf-8")
    reason = "Your output isn't accepted yet. Fix these problems, rewrite the file, then end again:\n- " + "\n- ".join(
        problems[:20]
    )
    print(json.dumps({"decision": "block", "reason": reason}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - the driver re-validates; never trap the lane in a loop
        print(f"output validator error: {e}", file=sys.stderr)
        sys.exit(0)
