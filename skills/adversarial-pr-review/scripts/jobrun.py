"""Detached job wrapper: run the command in <job dir>/spec.json with a timeout.

Writes stdout.log and stderr.log (never discarded) and finally exit.json with the exit code and a
status of done, failed or timeout. The driver reads exit.json; it does not trust the process.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


def main(job_dir: str) -> int:
    jdir = Path(job_dir)
    spec = json.loads((jdir / "spec.json").read_text(encoding="utf-8"))
    started = time.time()
    status, code = "failed", None
    stdin_file = spec.get("stdin_file")
    env = {k: v for k, v in os.environ.items() if k not in set(spec["env_unset"])}
    env.update(spec["env_overrides"])
    with (
        open(jdir / "stdout.log", "wb") as out,
        open(jdir / "stderr.log", "wb") as err,
        open(stdin_file or os.devnull, "rb") as stdin,
    ):
        try:
            proc = subprocess.Popen(
                spec["argv"],
                cwd=spec["cwd"],
                env=env,
                stdin=stdin,
                stdout=out,
                stderr=err,
                start_new_session=True,
            )
        except OSError as e:
            err.write(f"job wrapper: cannot start {spec['argv'][:1]}: {e}\n".encode())
        else:
            try:
                code = proc.wait(timeout=spec["timeout_s"])
                status = "done"
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait()
                status = "timeout"
                err.write(f"\njob wrapper: killed after {spec['timeout_s']}s timeout\n".encode())
    record = {"status": status, "exit_code": code, "started_at": started, "ended_at": time.time()}
    tmp = jdir / "exit.json.tmp"
    tmp.write_text(json.dumps(record, sort_keys=True), encoding="utf-8")
    os.replace(tmp, jdir / "exit.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
