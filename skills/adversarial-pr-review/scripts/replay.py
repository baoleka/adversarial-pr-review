"""Run repros in isolation (Docker: no network, read-only tree, no capabilities, scrubbed env).

    replay.py lane --run RUN --lane LANE [--side head|base|both] [--differential] FILE

The `lane` entry is the only command review lanes may run (the guard enforces the exact shape).
It copies FILE from the lane's scratch dir into a fresh export of each side's commit, runs it in a
container and prints the outcome as JSON. Records live outside the lane's scratch dir, so a lane
can't forge them.

Isolation: the interpreter and venv the gates used are bind-mounted read-only at their own paths;
the exported tree is mounted read-only at /work; only /tmp (a size-limited tmpfs) is writable.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import threading
import time
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

# Run as a script by the lanes' replay command: don't write __pycache__ into the install dir.
sys.dont_write_bytecode = True

from common import ReviewError, read_json, run, sha256_text, write_json

REPRO_NAME = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}\.py$")
LANE_OUTPUT_CHARS = 6000
WORKDIR = "/work"
_SIGNATURE = re.compile(r"^(?:E\s+)?([A-Za-z_][\w.]*(?:Error|Exception|Exit|Interrupt|Warning|Failure))(?::\s*(.*))?$")
_BARE_ASSERT = re.compile(r"^E\s+(assert\b.*)$")
_ADDRESS = re.compile(r"0x[0-9a-fA-F]+")
_TMP = re.compile(r"/tmp/[^\s'\"]+")
_TIMING = re.compile(r"\bin \d+\.\d+s\b")
REPRO_ERRORS = ("SyntaxError", "IndentationError", "ImportError", "ModuleNotFoundError", "NameError")


# ---------------------------------------------------------------- interpreter and isolation detection


@dataclass(frozen=True)
class PythonEnv:
    executable: str
    prefix: str
    mounts: tuple[tuple[str, str], ...]  # (host path, container path), all read-only
    problem: str | None = None


def describe_python(path_env: str | None = None) -> PythonEnv:
    """The interpreter gates run (`python` on PATH) and the read-only mounts it needs."""
    exe = shutil.which("python", path=path_env) or shutil.which("python3", path=path_env)
    if not exe:
        return PythonEnv("", "", (), "no python on PATH")
    if sys.platform != "linux":
        # Repros run in a Linux container with the host's interpreter mounted into it.
        return PythonEnv(exe, "", (), f"repros need a Linux host (this is {sys.platform})")
    probe = run(
        [exe, "-c", "import sys,json;print(json.dumps([sys.prefix,sys.base_prefix,sys._base_executable]))"],
        check=False,
    )
    if probe.returncode != 0:
        return PythonEnv(exe, "", (), f"python on PATH failed: {probe.stderr.strip()[-200:]}")
    prefix, base_prefix, base_exe = json.loads(probe.stdout)
    real_base = os.path.realpath(base_prefix)
    if real_base.startswith(("/usr", "/bin", "/lib")):
        return PythonEnv(exe, prefix, (), "the gates use a system interpreter, which can't be mounted into a container")
    mounts = {(real_base, real_base)}
    if os.path.realpath(prefix) != real_base:
        mounts.add((os.path.realpath(prefix), prefix))
    # A venv's python is a symlink that may pass through an alias of the install dir.
    alias = str(Path(base_exe).parent.parent)
    if os.path.realpath(alias) == real_base and alias != real_base:
        mounts.add((real_base, alias))
    if base_prefix != real_base:
        mounts.add((real_base, base_prefix))
    return PythonEnv(exe, prefix, tuple(sorted(mounts)))


def _docker(args: Sequence[str], timeout: float = 30) -> tuple[int, str]:
    """(exit code, stderr tail) of a docker CLI call; a hang or a missing CLI counts as a failure."""
    try:
        out = run(["docker", *args], check=False, timeout=timeout)
    except (subprocess.TimeoutExpired, ReviewError) as e:
        return 1, str(e)[-200:]
    return out.returncode, out.stderr.strip()[-200:]


def docker_daemon_problem() -> str | None:
    """None if the Docker CLI is installed and its daemon answers, else the reason."""
    if not shutil.which("docker"):
        return "docker is not installed"
    code, err = _docker(["info", "--format", "{{.ServerVersion}}"])
    return None if code == 0 else f"the docker daemon is not reachable: {err}"


def image_present(image: str) -> bool:
    return _docker(["image", "inspect", "--format", "{{.Id}}", image])[0] == 0


def docker_available(image: str) -> str | None:
    """None if Docker can run ``image`` here, else the reason it can't."""
    problem = docker_daemon_problem()
    if problem:
        return problem
    if not image_present(image):
        return f"docker image {image} is not available (pulled automatically when replay.auto_pull is on)"
    return None


def pull_needed(replay_cfg: Mapping[str, Any], python_problem: str | None) -> bool:
    """Whether to pull the pinned image now: replays could run here, but the image is missing."""
    if replay_cfg.get("isolation", "auto") not in ("auto", "docker") or not replay_cfg.get("auto_pull", True):
        return False
    if python_problem or sys.platform != "linux":
        return False
    return docker_daemon_problem() is None and not image_present(str(replay_cfg["image"]))


def pull_argv(image: str) -> list[str]:
    return ["docker", "pull", "--quiet", image]


def isolation_mode(replay_cfg: Mapping[str, Any]) -> tuple[str, str | None]:
    wanted = replay_cfg.get("isolation", "auto")
    if wanted in ("auto", "docker"):
        problem = docker_available(replay_cfg["image"])
        if problem is None:
            return "docker", None
        if wanted == "docker" or replay_cfg.get("require_network_isolation", True):
            return "none", problem
    if wanted == "bwrap":
        return "none", "bwrap isolation is not implemented on this host"
    if replay_cfg.get("require_network_isolation", True):
        return "none", "no isolation backend available and network isolation is required"
    return "none", None


# ---------------------------------------------------------------- tree export


def export_tree(repo: Path, commit: str, dest: Path) -> None:
    """Extract ``commit`` into ``dest`` (a pristine copy: no .git, no build residue)."""
    data = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar", commit], capture_output=True, check=False)
    if data.returncode != 0:
        raise ReviewError(f"git archive {commit} failed: {data.stderr.decode(errors='replace')[-300:]}")
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(data.stdout)) as tar:
        tar.extractall(dest, filter="data")


# ---------------------------------------------------------------- running


@dataclass
class SideOutcome:
    outcome: str  # pass | failed | error | timeout | not-run
    exit_code: int | None
    signature: str | None
    output_ref: str | None
    output_tail: str
    output_sha256: str | None
    duration_s: float
    truncated: bool = False
    runs: int = 1

    def to_json(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class ReplaySetup:
    image: str
    python: PythonEnv
    timeout_s: int
    output_cap: int
    python_paths: list[str] = field(default_factory=list)
    repro_dir: str = "tests"


def _run_capped(argv: Sequence[str], timeout_s: float, cap: int, name: str) -> tuple[int | None, bytes, bool, bool]:
    """Run ``argv``; keep at most ``cap`` bytes of combined output; kill the container on timeout."""
    proc = subprocess.Popen(list(argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    assert proc.stdout is not None
    if proc.stdin is not None:
        proc.stdin.close()
    chunks: list[bytes] = []
    size = [0, False]

    def drain() -> None:
        assert proc.stdout is not None
        for chunk in iter(lambda: proc.stdout.read(65536), b""):  # type: ignore[union-attr]
            if size[0] < cap:
                chunks.append(chunk[: cap - int(size[0])])
            if size[0] + len(chunk) > cap:
                size[1] = True
            size[0] = int(size[0]) + len(chunk)

    reader = threading.Thread(target=drain, daemon=True)
    reader.start()
    timed_out = False
    try:
        code: int | None = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        run(["docker", "kill", name], check=False, timeout=30)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        code = None
    reader.join(timeout=10)
    proc.stdout.close()
    run(["docker", "rm", "-f", name], check=False, timeout=30)
    return code, b"".join(chunks), bool(size[1]), timed_out


def docker_argv(setup: ReplaySetup, tree: Path, command: Sequence[str], name: str) -> list[str]:
    py = setup.python
    path = ":".join([str(Path(py.prefix) / "bin"), "/usr/local/bin", "/usr/bin", "/bin"])
    pythonpath = ":".join(f"{WORKDIR}/{p}" for p in setup.python_paths)
    argv = [
        "docker", "run", "--rm", "--name", name,
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=256m,mode=1777",
        "--user", f"{os.getuid()}:{os.getgid()}",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--pids-limit", "256",
        "--memory", "2g",
        "--cpus", "2",
        "--workdir", WORKDIR,
        "--mount", f"type=bind,source={tree},target={WORKDIR},readonly",
    ]  # fmt: skip
    for host, target in py.mounts:
        argv += ["--mount", f"type=bind,source={host},target={target},readonly"]
    env = {
        "PATH": path,
        "HOME": "/tmp",
        "LANG": "C.UTF-8",
        "PYTHONHASHSEED": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": pythonpath,
        "PYTEST_ADDOPTS": "-p no:cacheprovider",
    }
    for key in sorted(env):
        argv += ["--env", f"{key}={env[key]}"]
    return argv + [setup.image, *command]


def _clean(message: str) -> str:
    return _TMP.sub("<tmp>", _ADDRESS.sub("0x?", message.strip()))[:300]


def signature_of(output: str) -> str | None:
    """(exception type, normalized message) of the first failure in the output.

    pytest reports a failed plain `assert` as `E   assert <expr>` without an exception name; that
    is an AssertionError with the rewritten expression as its message.
    """
    for raw in output.splitlines():
        line = raw.strip()
        bare = _BARE_ASSERT.match(line)
        if bare:
            return f"AssertionError: {_clean(bare.group(1))}"
        match = _SIGNATURE.match(line)
        if match:
            message = _clean(match.group(2) or "")
            name = match.group(1).split(".")[-1]
            return f"{name}: {message}" if message else name
    return None


def classify_exit(command: Sequence[str], code: int | None, output: str, timed_out: bool) -> str:
    if timed_out:
        return "timeout"
    if code == 0:
        return "pass"
    is_pytest = "pytest" in command
    if is_pytest:
        # pytest: 1 = tests failed; 2 interrupted (collection errors), 3 internal, 4 usage, 5 no tests.
        if code == 1:
            return "failed"
        return "error"
    sig = signature_of(output) or ""
    if code is None or code >= 125 or any(sig.startswith(e) for e in REPRO_ERRORS):
        return "error"
    return "failed"


def command_for(filename: str, repro_rel: str) -> list[str]:
    if filename.startswith("test_") or filename.endswith("_test.py"):
        return ["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:randomly", repro_rel]
    return ["python", repro_rel]


def normalize_output(text: str) -> str:
    return _TIMING.sub("in ?s", _TMP.sub("<tmp>", _ADDRESS.sub("0x?", text)))


def run_side(
    setup: ReplaySetup,
    repo: Path,
    commit: str,
    filename: str,
    content: str,
    command: Sequence[str] | None,
    work: Path,
) -> SideOutcome:
    """Export ``commit``, add the repro, run it in a container; the export is deleted afterwards."""
    work = work.resolve()
    tree = work / "tree"
    if tree.exists():
        shutil.rmtree(tree)
    export_tree(repo, commit, tree)
    repro_dir = setup.repro_dir if (tree / setup.repro_dir).is_dir() else "."
    repro_rel = f"{repro_dir}/{filename}" if repro_dir != "." else filename
    (tree / repro_rel).write_text(content, encoding="utf-8")
    argv = [part.replace("{repro}", repro_rel) for part in command] if command else command_for(filename, repro_rel)
    name = f"adrev-{secrets.token_hex(6)}"
    started = time.time()
    try:
        code, raw, truncated, timed_out = _run_capped(
            docker_argv(setup, tree, argv, name), setup.timeout_s, setup.output_cap, name
        )
    finally:
        shutil.rmtree(tree, ignore_errors=True)
    output = raw.decode("utf-8", errors="replace")
    out_path = work / "output.txt"
    out_path.write_text(output, encoding="utf-8")
    outcome = classify_exit(argv, code, output, timed_out)
    return SideOutcome(
        outcome=outcome,
        exit_code=code,
        signature=signature_of(output) if outcome != "pass" else None,
        output_ref=str(out_path),
        output_tail=output[-LANE_OUTPUT_CHARS:],
        output_sha256=sha256_text(normalize_output(output)),
        duration_s=round(time.time() - started, 2),
        truncated=truncated,
    )


def not_run(reason: str) -> SideOutcome:
    return SideOutcome("not-run", None, None, None, reason, None, 0.0, runs=0)


# ---------------------------------------------------------------- run-state access (read-only)


def setup_from_run(run_dir: Path) -> tuple[ReplaySetup | None, str | None, dict[str, Any]]:
    state = read_json(run_dir / "state.json")
    cfg = read_json(run_dir / "config.json")
    replay_cfg = cfg["replay"]
    mode, problem = isolation_mode(replay_cfg)
    if mode != "docker":
        return None, problem or "replay isolation unavailable", state
    py_json = state.get("replay_python")
    if not py_json:
        return None, "the driver has not recorded the gate interpreter yet", state
    mounts = tuple((str(host), str(target)) for host, target in py_json["mounts"])
    py = PythonEnv(py_json["executable"], py_json["prefix"], mounts, py_json.get("problem"))
    if py.problem:
        return None, py.problem, state
    setup = ReplaySetup(
        image=replay_cfg["image"],
        python=py,
        timeout_s=int(replay_cfg["timeout_s"]),
        output_cap=int(replay_cfg["output_cap_bytes"]),
        python_paths=list(cfg["repo"].get("python_paths", [])),
        repro_dir=str(replay_cfg.get("repro_dir", "tests")),
    )
    return setup, None, state


def _next_slot(base: Path) -> Path:
    base.mkdir(parents=True, exist_ok=True)
    for index in range(1, 10_000):
        slot = base / f"{index:03d}"
        try:
            slot.mkdir()
            return slot
        except FileExistsError:
            continue
    raise ReviewError("too many replays")


def cmd_lane(args: argparse.Namespace) -> int:
    run_dir = Path(args.run).resolve()
    lane = args.lane
    if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{0,80}", lane):
        return _fail("bad lane id")
    scratch = (run_dir / "agents" / lane).resolve()
    repro_path = Path(args.file)
    repro_path = (repro_path if repro_path.is_absolute() else Path.cwd() / repro_path).resolve()
    if not repro_path.is_relative_to(scratch) or not repro_path.is_file():
        return _fail("the repro file must be an existing file inside your scratch dir")
    if not REPRO_NAME.match(repro_path.name):
        return _fail("repro file names must match ^[A-Za-z0-9_][A-Za-z0-9_.-]{0,80}\\.py$")
    content = repro_path.read_text(encoding="utf-8", errors="replace")
    if len(content) > 20_000:
        return _fail("repro files are limited to 20000 characters")
    setup, problem, state = setup_from_run(run_dir)
    if setup is None:
        return _print({"error": f"replay unavailable: {problem}. Report traced evidence instead."})
    repo = Path(state["repo"])
    round_no = str(state["round"])
    commits = {"head": state["rounds"][round_no]["snapshot"]["commit"], "base": state["merge_base"]}
    sides = ["head", "base"] if args.side == "both" else [args.side]
    slot = _next_slot(run_dir / "replays" / "lanes" / lane)
    (slot / repro_path.name).write_text(content, encoding="utf-8")
    result: dict[str, Any] = {"replay": f"{lane}/{slot.name}", "file": repro_path.name}
    for side in sides:
        outcome = run_side(setup, repo, commits[side], repro_path.name, content, None, slot / side)
        result[side] = outcome.to_json()
    if args.differential and len(sides) == 2:
        result["differential"] = {
            "outputs_equal": result["head"]["output_sha256"] == result["base"]["output_sha256"],
            "outcomes": [result["head"]["outcome"], result["base"]["outcome"]],
        }
    record = {**result, "repro_sha256": sha256_text(content), "round": int(round_no), "command": "lane"}
    write_json(slot / "record.json", record)
    visible = {k: v for k, v in result.items()}
    for side in sides:
        visible[side] = {k: v for k, v in result[side].items() if k not in ("output_ref", "output_sha256")}
    return _print(visible)


# ---------------------------------------------------------------- driver-side replay of findings

CLAIMED_TYPES = ("AssertionError", "Failed")  # the repro's own assertion; pytest's "DID NOT RAISE"
MISSING_SYMBOL = re.compile(r"(?:cannot import name|has no attribute|name) '([A-Za-z_][\w]*)'")


def repro_plan(repro: Mapping[str, Any], allow: Sequence[Sequence[str]]) -> tuple[str, list[str]] | None:
    """(file name, command) for a finding's repro, or None if it may not run.

    The command comes from the repro kind, never from the lane: `test` runs under pytest, the
    others as a script. It must be listed in `replay.allow`.
    """
    name = str(repro.get("filename", ""))
    if not REPRO_NAME.match(name):
        return None
    if repro.get("kind") == "test":
        name = name if name.startswith("test_") else f"test_{name}"
        base_form = ["python", "-m", "pytest", "{repro}"]
        command = ["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:randomly", "{repro}"]
    else:
        base_form = ["python", "{repro}"]
        command = base_form
    if [list(a) for a in allow].count(base_form) == 0:
        return None
    return name, command


def claimed(outcome: SideOutcome, repro: Mapping[str, Any]) -> bool:
    """A failure counts as claimed when it is the repro's own assertion or the failure it names."""
    if outcome.outcome != "failed":
        return False
    if outcome.signature is None:
        return True
    kind = outcome.signature.split(":", 1)[0]
    expected = str(repro.get("expected_failure") or "")
    return kind in CLAIMED_TYPES or (bool(expected) and kind.lower() in expected.lower())


def side_json(runs: Sequence[SideOutcome], repro: Mapping[str, Any]) -> dict[str, Any]:
    last = runs[-1] if runs else not_run("not run")
    outcome = {"pass": "pass", "timeout": "timeout", "error": "error", "not-run": "not-run"}.get(last.outcome)
    if last.outcome == "failed":
        outcome = "fail-as-claimed" if claimed(last, repro) else "error"
    return {
        "outcome": outcome,
        "signature": last.signature,
        "exit_code": last.exit_code,
        "output_ref": last.output_ref,
        "runs": sum(r.runs for r in runs),
    }


def classify(
    repro: Mapping[str, Any], head: Sequence[SideOutcome], base: Sequence[SideOutcome], added_text: str
) -> tuple[str, str]:
    if repro.get("kind") == "differential":
        sides = {"head": head, "base": base}
        for name, runs in sides.items():
            if len(runs) < 2 or any(r.outcome in ("error", "timeout", "not-run") for r in runs):
                return "not-demonstrated", f"the differential repro did not run cleanly twice at {name}"
            if len({r.output_sha256 for r in runs}) != 1:
                return "not-demonstrated", f"the output at {name} is not stable across 2 runs"
        if head[0].output_sha256 == base[0].output_sha256:
            return "not-demonstrated", "head and base print the same output"
        return "introduced", "head and base outputs are stable and differ"
    h, b = head[-1], base[-1]
    if not claimed(h, repro):
        return "not-demonstrated", f"at head the repro {'passes' if h.outcome == 'pass' else 'ends in ' + h.outcome}"
    if b.outcome == "pass":
        return "introduced", "fails as claimed at head and passes at base"
    if claimed(b, repro):
        if b.signature == h.signature:
            return "pre-existing", "fails the same way at base"
        return "not-demonstrated", "fails at both sides with different signatures"
    missing = MISSING_SYMBOL.search(b.signature or "")
    if b.outcome == "error" and missing and re.search(rf"\b{re.escape(missing.group(1))}\b", added_text):
        return "introduced", f"fails as claimed at head; base can't import {missing.group(1)}, which the change adds"
    return "not-demonstrated", f"at base the repro ends in {b.outcome}"


def replay_finding(
    setup: ReplaySetup,
    repo: Path,
    commits: Mapping[str, str],
    finding: Mapping[str, Any],
    allow: Sequence[Sequence[str]],
    added_text: str,
    work: Path,
) -> dict[str, Any]:
    repro = finding["evidence"]["repro"]
    plan = repro_plan(repro, allow)
    if plan is None:
        none = side_json([], repro)
        return {
            "head": none,
            "base": none,
            "classification": "not-replayed",
            "reason": "the repro may not run (name or command not allowed)",
        }
    name, command = plan
    runs: dict[str, list[SideOutcome]] = {}

    def once(side: str, index: int) -> SideOutcome:
        return run_side(setup, repo, commits[side], name, repro["content"], command, work / f"{side}-{index}")

    for side in ("head", "base"):
        if repro.get("kind") == "differential":
            # Stability is part of the evidence: two runs per side.
            runs[side] = [once(side, 1), once(side, 2)]
        else:
            runs[side] = [once(side, 1)]
            if runs[side][0].outcome in ("error", "timeout"):
                runs[side].append(once(side, 2))
    classification, reason = classify(repro, runs["head"], runs["base"], added_text)
    return {
        "head": side_json(runs["head"], repro),
        "base": side_json(runs["base"], repro),
        "classification": classification,
        "reason": reason,
    }


def cmd_batch(args: argparse.Namespace) -> int:
    """Replay every lane finding of a round that carries a repro; write the results as JSON."""
    run_dir = Path(args.run).resolve()
    setup, problem, state = setup_from_run(run_dir)
    store = read_json(run_dir / "findings.json")
    cfg = read_json(run_dir / "config.json")
    round_no = str(args.round)
    commits = {"head": state["rounds"][round_no]["snapshot"]["commit"], "base": state["merge_base"]}
    added_text = Path(args.added).read_text(encoding="utf-8") if args.added else ""
    results: dict[str, Any] = {}
    chosen = read_json(Path(args.findings)) if args.findings else store["rounds"][round_no]["lane"]
    for finding in chosen:
        if not finding["evidence"]["repro"]:
            continue
        if setup is None:
            none = side_json([], finding["evidence"]["repro"])
            results[finding["id"]] = {
                "head": none,
                "base": none,
                "classification": "not-replayed",
                "reason": f"replay unavailable: {problem}",
            }
            continue
        work = run_dir / "replays" / f"r{round_no}" / finding["id"]
        results[finding["id"]] = replay_finding(
            setup, Path(state["repo"]), commits, finding, cfg["replay"]["allow"], added_text, work
        )
    write_json(Path(args.out), results)
    return 0


def _print(data: Mapping[str, Any]) -> int:
    print(json.dumps(data, indent=1, sort_keys=True))
    return 0


def _fail(message: str) -> int:
    print(json.dumps({"error": message}))
    return 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="replay.py")
    sub = parser.add_subparsers(dest="cmd", required=True)
    lane = sub.add_parser("lane")
    lane.add_argument("--run", required=True)
    lane.add_argument("--lane", required=True)
    lane.add_argument("--side", choices=("head", "base", "both"), default="both")
    lane.add_argument("--differential", action="store_true")
    lane.add_argument("file")
    batch = sub.add_parser("batch")
    batch.add_argument("--run", required=True)
    batch.add_argument("--round", type=int, required=True)
    batch.add_argument("--out", required=True)
    batch.add_argument("--added", help="file with the text of the change's added lines")
    batch.add_argument("--findings", help="JSON list of findings to replay instead of the round's lane findings")
    args = parser.parse_args(argv)
    try:
        if args.cmd == "lane":
            return cmd_lane(args)
        if args.cmd == "batch":
            return cmd_batch(args)
    except ReviewError as e:
        return _fail(str(e))
    return 2


if __name__ == "__main__":
    sys.exit(main())
