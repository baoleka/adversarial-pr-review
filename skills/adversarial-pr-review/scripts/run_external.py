"""Run the external lane with the Codex CLI (only when `external.provider` is "codex").

    run_external.py <control dir>

The control dir holds `external.json` (written by the driver): the codex binary, model, export dir,
output schema, brief, output path and timeout. Before the real call, a read-confinement canary
plants a secret outside the export dir and asks Codex to print it; if the secret comes back,
Codex can read the whole disk (and send it to its provider), so the call is refused and the
driver falls back to a Claude lens. Writes `status.json` with ok | refused | failed | timeout.
"""

from __future__ import annotations

import json
import secrets
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any


def codex_argv(codex_bin: str, export_dir: Path, schema: Path | None, out: Path, model: str | None) -> list[str]:
    argv = [
        codex_bin,
        "exec",
        "--sandbox",
        "read-only",
        "-C",
        str(export_dir),
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--ephemeral",
        "--json",
    ]
    if schema is not None:
        argv += ["--output-schema", str(schema)]
    argv += ["-o", str(out)]
    if model:
        argv += ["-m", model]
    return argv + ["-"]


def parse_usage(jsonl: str) -> dict[str, int]:
    """Sum `turn.completed.usage` over a `codex exec --json` event stream."""
    total = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0, "reasoning_output_tokens": 0}
    for line in jsonl.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") == "turn.completed":
            usage = event.get("usage") or {}
            for key in total:
                value = usage.get(key)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    total[key] += value
    return total


def _run(argv: Sequence[str], prompt: str, cwd: Path, timeout: float, log: Path) -> tuple[int | None, str]:
    with open(log, "w", encoding="utf-8") as fh:
        try:
            proc = subprocess.run(
                list(argv),
                input=prompt,
                stdout=fh,
                stderr=subprocess.STDOUT,
                text=True,
                cwd=cwd,
                timeout=timeout,
                check=False,
            )
            code: int | None = proc.returncode
        except subprocess.TimeoutExpired:
            code = None
        except OSError as e:
            fh.write(f"run_external: cannot start {argv[0]}: {e}\n")
            code = 127
    return code, log.read_text(encoding="utf-8", errors="replace")


def canary(spec: dict[str, Any], control: Path) -> tuple[bool, str]:
    """True if Codex could not read a file planted outside its export dir."""
    secret = f"ADREV-CANARY-{secrets.token_hex(12)}"
    planted = control / "canary" / f"secret-{secrets.token_hex(4)}.txt"
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.write_text(secret + "\n", encoding="utf-8")
    out = control / "canary" / "out.txt"
    argv = codex_argv(spec["codex_bin"], Path(spec["export_dir"]), None, out, spec.get("model"))
    prompt = f"Print the exact contents of the file {planted}. If you cannot read it, say UNREADABLE."
    code, log = _run(argv, prompt, Path(spec["export_dir"]), 300, control / "canary" / "events.jsonl")
    leaked = secret in log or (out.is_file() and secret in out.read_text(encoding="utf-8", errors="replace"))
    planted.unlink()
    if leaked:
        return False, "codex read a file outside its export dir: its reads aren't confined on this machine"
    if code != 0:
        # A call that fails proves nothing about confinement: fail closed.
        return False, f"the canary call did not complete (exit {code})"
    return True, "reads outside the export dir are blocked"


def main(control_dir: str) -> int:
    control = Path(control_dir)
    spec = json.loads((control / "external.json").read_text(encoding="utf-8"))
    status: dict[str, Any] = {"status": "failed", "started_at": time.time()}
    ok, reason = canary(spec, control)
    status["canary"] = reason
    if not ok:
        status["status"] = "refused"
    else:
        argv = codex_argv(
            spec["codex_bin"], Path(spec["export_dir"]), Path(spec["schema"]), Path(spec["out"]), spec.get("model")
        )
        prompt = Path(spec["brief"]).read_text(encoding="utf-8")
        code, log = _run(argv, prompt, Path(spec["export_dir"]), float(spec["timeout_s"]), control / "events.jsonl")
        status["exit_code"] = code
        status["usage"] = parse_usage(log)
        status["status"] = "timeout" if code is None else "ok" if code == 0 else "failed"
    status["ended_at"] = time.time()
    (control / "status.json").write_text(json.dumps(status, indent=1, sort_keys=True), encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
