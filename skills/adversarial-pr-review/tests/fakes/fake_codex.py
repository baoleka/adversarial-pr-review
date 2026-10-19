"""A stand-in for `codex exec` in tests: same flags, JSONL events, no model.

$FAKE_CODEX_SCENARIO: `leaky` (default: like `--sandbox read-only`, any file is readable),
`confined` (reads outside -C fail), or `fail` (exit 1).
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def main() -> int:
    import os

    args = sys.argv[1:]
    scenario = os.environ.get("FAKE_CODEX_SCENARIO", "leaky")
    if scenario == "fail":
        print("fake codex: failing", file=sys.stderr)
        return 1
    workdir = Path(args[args.index("-C") + 1]).resolve()
    out = Path(args[args.index("-o") + 1])
    prompt = sys.stdin.read()
    print(json.dumps({"type": "thread.started", "thread_id": "fake"}))
    asked = re.search(r"contents of the file (\S+)\.", prompt)
    if asked:
        target = Path(asked.group(1)).resolve()
        inside = target.is_relative_to(workdir)
        text = target.read_text() if target.is_file() and (inside or scenario == "leaky") else "UNREADABLE"
        print(json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": text}}))
        out.write_text(text)
    else:
        out.write_text(json.dumps({"findings": [], "notes": None}))
    usage = {"input_tokens": 1200, "cached_input_tokens": 800, "output_tokens": 300, "reasoning_output_tokens": 120}
    print(json.dumps({"type": "turn.completed", "usage": usage}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
