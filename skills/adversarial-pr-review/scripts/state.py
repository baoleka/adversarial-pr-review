"""Run directory, driver state, tamper detection, actions and detached jobs.

Only the driver writes the state files (state.json, findings.json, verdict.json, ledger.json).
Every write records the file's sha256 in hashes.json; any later mismatch means something else
changed run state, and the run ends in ESCALATE.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from common import (
    SCRIPTS_DIR,
    ReviewError,
    append_jsonl,
    canonical_json,
    read_json,
    sha256_file,
    sha256_text,
    write_json,
)

STATE_FILES = ("state.json", "findings.json", "verdict.json", "ledger.json")
SCHEMA_VERSION = "1"


class TamperError(ReviewError):
    """A state file changed outside the driver."""


def new_run_id() -> str:
    return f"{dt.datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Run:
    def __init__(self, run_dir: Path) -> None:
        self.dir = run_dir
        self.state: dict[str, Any] = {}

    # ------------------------------------------------------------------ paths

    @property
    def hashes_path(self) -> Path:
        return self.dir / "hashes.json"

    def round_dir(self, round_no: int | None = None) -> Path:
        path = self.dir / "rounds" / f"r{round_no or self.state['round']}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def agent_dir(self, lane_id: str) -> Path:
        path = self.dir / "agents" / lane_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    def job_dir(self, job_id: str) -> Path:
        path = self.dir / "jobs" / job_id
        path.mkdir(parents=True, exist_ok=True)
        return path

    @property
    def wt_base(self) -> Path:
        return self.dir / "wt" / "base"

    @property
    def wt_head(self) -> Path:
        return self.dir / "wt" / "head"

    @property
    def no_hooks(self) -> Path:
        path = self.dir / "no-hooks"
        path.mkdir(parents=True, exist_ok=True)
        return path

    # ------------------------------------------------------------------ lifecycle

    @classmethod
    def create(cls, run_root: Path, run_id: str, initial: Mapping[str, Any]) -> Run:
        run_dir = run_root / run_id
        if run_dir.exists():
            raise ReviewError(f"run {run_id} already exists")
        run_dir.mkdir(parents=True)
        run = cls(run_dir)
        run.state = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "created_at": now_iso(), **initial}
        run.state.setdefault("actions", [])
        run.state.setdefault("jobs", {})
        run.state.setdefault("seq", 0)
        run.save()
        (run_root / "CURRENT").write_text(run_id + "\n", encoding="utf-8")
        return run

    @classmethod
    def load(cls, run_dir: Path) -> Run:
        run = cls(run_dir)
        problems = run.integrity_problems()
        if problems:
            raise TamperError("; ".join(problems))
        run.state = read_json(run_dir / "state.json")
        return run

    @staticmethod
    def current(run_root: Path) -> Path | None:
        pointer = run_root / "CURRENT"
        if not pointer.is_file():
            return None
        run_dir = run_root / pointer.read_text(encoding="utf-8").strip()
        return run_dir if (run_dir / "state.json").is_file() else None

    # ------------------------------------------------------------------ persistence

    def recorded_hashes(self) -> dict[str, str]:
        if not self.hashes_path.is_file():
            return {}
        data = read_json(self.hashes_path)
        return {str(k): str(v) for k, v in data.items()}

    def integrity_problems(self) -> list[str]:
        recorded = self.recorded_hashes()
        problems = []
        for name in STATE_FILES:
            path = self.dir / name
            if name in recorded:
                if not path.is_file():
                    problems.append(f"{name} was deleted")
                elif sha256_file(path) != recorded[name]:
                    problems.append(f"{name} was modified outside the driver")
            elif path.exists():
                problems.append(f"{name} appeared outside the driver")
        return problems

    def write_state_file(self, name: str, data: Any) -> None:
        if name not in STATE_FILES:
            raise ValueError(f"{name} is not a state file")
        recorded = self.recorded_hashes()
        recorded[name] = write_json(self.dir / name, data)
        write_json(self.hashes_path, recorded)

    def save(self) -> None:
        self.write_state_file("state.json", self.state)

    def state_hash(self) -> str:
        """Hash of the semantic state, ignoring the action bookkeeping itself."""
        view = {k: v for k, v in self.state.items() if k not in ("actions", "seq")}
        return sha256_text(canonical_json(view))

    def event(self, event_kind: str, **data: Any) -> None:
        append_jsonl(self.dir / "events.jsonl", {"at": now_iso(), "event": event_kind, **data})

    def next_seq(self) -> int:
        self.state["seq"] = int(self.state.get("seq", 0)) + 1
        return int(self.state["seq"])

    # ------------------------------------------------------------------ jobs

    def spawn_job(
        self,
        kind: str,
        argv: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        timeout_s: int,
        meta: Mapping[str, Any] | None = None,
        stdin_file: Path | None = None,
    ) -> str:
        """Start ``argv`` detached (new session) under the job wrapper; return the job id."""
        job_id = f"j{self.next_seq():04d}-{kind}"
        jdir = self.job_dir(job_id)
        # Store only the differences from the inherited environment, so credentials in the
        # environment (for example an OAuth token) are never written to the run dir.
        overrides = {k: v for k, v in env.items() if os.environ.get(k) != v}
        unset = sorted(k for k in os.environ if k not in env)
        spec = {
            "argv": list(argv),
            "cwd": str(cwd),
            "env_overrides": overrides,
            "env_unset": unset,
            "timeout_s": timeout_s,
            "stdin_file": str(stdin_file) if stdin_file else None,
        }
        (jdir / "spec.json").write_text(canonical_json(spec), encoding="utf-8")
        wrapper = [sys.executable, "-E", "-s", str(SCRIPTS_DIR / "jobrun.py"), str(jdir)]
        with open(jdir / "wrapper.out", "wb") as out, open(jdir / "wrapper.err", "wb") as err:
            proc = subprocess.Popen(
                wrapper, cwd=str(cwd), stdin=subprocess.PIPE, stdout=out, stderr=err, start_new_session=True
            )
        if proc.stdin is not None:
            proc.stdin.close()
        self.state["jobs"][job_id] = {
            "kind": kind,
            "pid": proc.pid,
            "started_at": time.time(),
            "timeout_s": timeout_s,
            "status": "running",
            "exit_code": None,
            "meta": dict(meta or {}),
        }
        self.event("job-start", job=job_id, job_kind=kind, argv=list(argv)[:6])
        return job_id

    def poll_job(self, job_id: str) -> str:
        """Update and return the job's status from its exit record or process state."""
        job = self.state["jobs"][job_id]
        if job["status"] != "running":
            return str(job["status"])
        exit_file = self.dir / "jobs" / job_id / "exit.json"
        if exit_file.is_file():
            record = json.loads(exit_file.read_text(encoding="utf-8"))
            job["exit_code"] = record.get("exit_code")
            job["status"] = record.get("status", "done")
            job["ended_at"] = record.get("ended_at")
            self.event("job-end", job=job_id, status=job["status"], exit_code=job["exit_code"])
            return str(job["status"])
        alive = _pid_alive(int(job["pid"]))
        overdue = time.time() - float(job["started_at"]) > float(job["timeout_s"]) + 60
        if not alive:
            job["status"] = "failed"
            job["exit_code"] = None
            self.event("job-lost", job=job_id)
        elif overdue:
            _kill_group(int(job["pid"]))
            job["status"] = "timeout"
            self.event("job-killed", job=job_id)
        return str(job["status"])

    def job_output(self, job_id: str, stream: str = "stdout") -> str:
        path = self.dir / "jobs" / job_id / f"{stream}.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    def running_jobs(self) -> list[str]:
        return [jid for jid, job in self.state["jobs"].items() if job["status"] == "running"]


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    # A zombie still answers kill(0); check /proc where available.
    status = Path(f"/proc/{pid}/status")
    if status.is_file():
        for line in status.read_text(encoding="utf-8", errors="replace").splitlines():
            if line.startswith("State:") and "Z" in line.split()[1]:
                return False
    return True


def _kill_group(pid: int) -> None:
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
