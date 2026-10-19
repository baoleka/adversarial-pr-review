"""Run one gate on one side: the CI command, then the optional diagnostics command.

Usage: gatejob.py <spec.json>. The spec gives cwd, argv, optional diagnostics_argv and an output
directory; the environment is the job's own (set by the job wrapper, never stored in the spec). Writes main.out, main.err, diag.out, diag.err and result.json there.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path


def run(argv: list[str], cwd: str, env: dict[str, str], out: Path, err: Path) -> int | None:
    with open(out, "wb") as fo, open(err, "wb") as fe:
        try:
            proc = subprocess.run(argv, cwd=cwd, env=env, stdin=subprocess.PIPE, stdout=fo, stderr=fe, check=False)
        except OSError as e:
            fe.write(f"gatejob: cannot run {argv[0]}: {e}\n".encode())
            return None
    return proc.returncode


def main(spec_path: str) -> int:
    spec = json.loads(Path(spec_path).read_text(encoding="utf-8"))
    out_dir = Path(spec["out_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    env = dict(os.environ)
    main_code = run(spec["argv"], spec["cwd"], env, out_dir / "main.out", out_dir / "main.err")
    for rel in spec.get("collect_files", []):
        produced = Path(spec["cwd"]) / rel
        if produced.is_file():
            os.replace(produced, out_dir / Path(rel).name)
    diag_code = None
    if spec.get("diagnostics_argv"):
        diag_code = run(spec["diagnostics_argv"], spec["cwd"], env, out_dir / "diag.out", out_dir / "diag.err")
    result = {"main_exit": main_code, "diag_exit": diag_code, "seconds": round(time.time() - started, 2)}
    tmp = out_dir / "result.json.tmp"
    tmp.write_text(json.dumps(result, sort_keys=True), encoding="utf-8")
    os.replace(tmp, out_dir / "result.json")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
