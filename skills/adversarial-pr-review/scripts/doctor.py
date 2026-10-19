"""`review.py doctor`: check that this machine can run a review, and say how to fix what can't.

Prints one JSON object: `{"version", "skill_dir", "ready", "checks": [...]}`. Each check is
`{"name", "required", "ok", "detail", "fix"}`, where `ok` is true, false, or null (skipped or not
applicable). `ready` is false when a required check is false. Repros (Docker) are optional: without
them a review still runs, with traced evidence only.

Unless `--offline`, the live check runs the guard self-test and one real probe lane (a few cents of
Haiku) in a throwaway directory. It proves in one go that `claude` is on PATH, authenticates, keeps
the lane flags and runs the lane hooks. `--models` also makes a tiny call to every configured model.
Nothing is written to the repository; the account e-mail and organization are never printed.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import lanes
import phases_lanes
import replay
from engine import Ctx
from lanes import LaneSpec

import config as config_mod
import snapshot as snap
from common import SKILL_DIR, VERSION, ReviewError, run, write_json
from state import Run

MIN_CLAUDE = (2, 1, 295)  # `onFailure` on hooks (lane guard), --restricted, --permission-prompts
AUTH_FIELDS = ("loggedIn", "authMethod", "apiProvider", "subscriptionType")


def _check(name: str, ok: bool | None, detail: str, fix: str | None = None, required: bool = True) -> dict[str, Any]:
    return {"name": name, "required": required, "ok": ok, "detail": detail, "fix": None if ok else fix}


def _version(text: str) -> tuple[int, ...] | None:
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
    return tuple(int(g) for g in match.groups()) if match else None


def lane_env() -> dict[str, str]:
    """The environment lanes start with (the outer session's, minus what lanes.py strips)."""
    return {k: v for k, v in os.environ.items() if k not in lanes.UNSET_ENV}


# ---------------------------------------------------------------- local checks


def check_python() -> dict[str, Any]:
    v = sys.version_info
    ok = v >= (3, 10) and hasattr(tarfile, "data_filter")
    return _check(
        "python",
        ok,
        f"Python {v.major}.{v.minor}.{v.micro} ({sys.executable})",
        "Install Python 3.12 or later as `python3` on PATH.",
    )


def check_paths(repo: Path | None) -> dict[str, Any]:
    spaced = [str(p) for p in (SKILL_DIR, repo) if p is not None and " " in str(p)]
    return _check(
        "paths",
        not spaced,
        "no spaces in the skill or repository path" if not spaced else "spaces in: " + ", ".join(spaced),
        "Use a repository (and skill install) path without spaces; lane commands can't carry them.",
    )


def check_git() -> dict[str, Any]:
    out = run(["git", "--version"], check=False)
    found = _version(out.stdout + ".0")
    ok = found is not None and found[:2] >= (2, 31)
    return _check("git", ok, out.stdout.strip() or "git not found", "Install git 2.31 or later.")


def check_repository(start: Path) -> tuple[list[dict[str, Any]], Path | None, dict[str, Any]]:
    """Repository, history depth, base ref, merge-base and config; also returns the loaded config."""
    try:
        root = snap.repo_root(start)
    except ReviewError:
        skipped = _check("repository", None, f"{start} is not inside a git repository; repository checks skipped")
        return [skipped], None, config_mod.DEFAULTS
    checks = [_check("repository", True, str(root))]
    shallow = snap.is_shallow(root)
    checks.append(
        _check(
            "history",
            not shallow,
            "shallow clone" if shallow else "full history",
            "Fetch the full history: `git fetch --unshallow origin`.",
            required=False,
        )
    )
    base_ref = snap.resolve_base_ref(root, None, "origin")
    if base_ref is None:
        checks.append(
            _check(
                "base ref",
                False,
                "none of origin/HEAD, origin/main, origin/master exists",
                "Fetch the base branch (e.g. `git fetch origin main:refs/remotes/origin/main`) or pass --base-ref.",
            )
        )
        return checks, root, config_mod.DEFAULTS
    head = "HEAD" if snap.head_commit(root) else None
    bases = snap.merge_bases(root, base_ref, head) if head else []
    checks.append(_check("base ref", True, base_ref))
    checks.append(
        _check(
            "merge-base",
            len(bases) == 1,
            f"{len(bases)} merge-base(s) between {base_ref} and HEAD",
            "Fetch more history (`git fetch --unshallow origin`)"
            if shallow
            else "Pick a base with a single merge-base (--base-ref), or rebase the branch.",
        )
    )
    cfg: dict[str, Any] = config_mod.DEFAULTS
    try:
        loaded = config_mod.load(root, bases[0] if len(bases) == 1 else None, head_root=root)
        cfg = loaded.data
        detail = f"{loaded.source}: {loaded.path}" if loaded.path else "no review.config.json: lanes only, no gates"
        checks.append(_check("config", True, detail))
    except ReviewError as e:
        checks.append(_check("config", False, str(e)[:500], "Fix review.config.json (see schemas/config.schema.json)."))
    return checks, root, cfg


def check_claude(cfg: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    """`claude` on PATH, its version, and the login lanes will use."""
    binary = str(cfg["limits"].get("claude_bin", "claude"))
    if not shutil.which(binary):
        return [
            _check("claude", False, f"`{binary}` is not on PATH", "Install Claude Code; lanes run `claude -p`.")
        ], False
    out = run([binary, "--version"], check=False, timeout=60)
    found = _version(out.stdout)
    ok = found is not None and found >= MIN_CLAUDE
    checks = [
        _check(
            "claude",
            ok,
            out.stdout.strip() or out.stderr.strip()[:200],
            f"Update Claude Code to {'.'.join(map(str, MIN_CLAUDE))} or later (`claude update`).",
        )
    ]
    auth = run([binary, "auth", "status", "--json"], env=lane_env(), replace_env=True, check=False, timeout=60)
    try:
        data = json.loads(auth.stdout)
    except ValueError:
        data = {}
    shown = {k: data[k] for k in AUTH_FIELDS if k in data}
    logged_in = bool(data.get("loggedIn"))
    checks.append(
        _check(
            "auth",
            logged_in,
            json.dumps(shown, sort_keys=True) if shown else (auth.stderr.strip()[:200] or "no auth status"),
            "Log in with `claude auth login` (lanes use the same login as this session).",
        )
    )
    remote = os.environ.get("CLAUDE_CODE_REMOTE") == "true"
    checks.append(_check("session", None, "cloud session" if remote else "local session"))
    return checks, ok and logged_in


def stable_install_root(skill_dir: Path) -> Path:
    """The directory to allow reads from: for a plugin in Claude Code's cache, the plugin's folder
    (it holds every version), else the plugin root, else the skill dir itself."""
    parts = skill_dir.parts
    for i, part in enumerate(parts[:-2]):
        if part == "plugins" and parts[i + 1] == "cache" and len(parts) > i + 4:
            return Path(*parts[: i + 4])
    for parent in (skill_dir, *skill_dir.parents):
        if (parent / ".claude-plugin").is_dir():
            return parent
    return skill_dir


def _settings_files(repo: Path | None) -> list[Path]:
    home = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude")
    files = [home / "settings.json", home / "settings.local.json"]
    if repo is not None:
        files += [repo / ".claude" / "settings.json", repo / ".claude" / "settings.local.json"]
    return files


def check_read_scope(repo: Path | None) -> dict[str, Any]:
    """With `blockReadsOutsideWorkingDirectories`, the skill's own folder must be a working dir."""
    blocked = False
    allowed: list[Path] = [repo] if repo is not None else []
    for path in _settings_files(repo):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        perms = data.get("permissions") if isinstance(data, dict) else None
        if not isinstance(perms, dict):
            continue
        blocked = blocked or perms.get("blockReadsOutsideWorkingDirectories") is True
        for entry in perms.get("additionalDirectories") or []:
            if isinstance(entry, str):
                allowed.append(Path(os.path.expanduser(entry)))
    if not blocked:
        return _check("read scope", True, "blockReadsOutsideWorkingDirectories is off")
    real = SKILL_DIR.resolve()
    covered = any(real == a.resolve() or a.resolve() in real.parents for a in allowed)
    suggestion = stable_install_root(SKILL_DIR)
    return _check(
        "read scope",
        covered,
        "blockReadsOutsideWorkingDirectories is on; the skill folder is "
        + ("a working directory" if covered else "outside every working directory"),
        f'Add "{suggestion}" to permissions.additionalDirectories in ~/.claude/settings.json, then restart '
        "Claude Code.",
    )


# ---------------------------------------------------------------- repros (optional)


def check_replay(cfg: dict[str, Any], live: bool) -> list[dict[str, Any]]:
    replay_cfg = cfg["replay"]
    if replay_cfg.get("isolation") == "none":
        return [_check("repros", None, "replay isolation is set to none in the config", required=False)]
    py = replay.describe_python()
    checks = [
        _check(
            "repro interpreter",
            py.problem is None,
            py.problem or f"{py.executable} (prefix {py.prefix})",
            "Put a uv-, pyenv- or conda-managed Python first on PATH (for example `uv venv` and activate it); "
            "a system Python can't be mounted into the repro container.",
            required=False,
        )
    ]
    daemon = replay.docker_daemon_problem()
    checks.append(
        _check(
            "docker",
            daemon is None,
            daemon or "daemon reachable",
            "Install Docker and start its daemon (cloud sessions: see the README's cloud setup).",
            required=False,
        )
    )
    if daemon is not None:
        return checks
    image = str(replay_cfg["image"])
    present = replay.image_present(image)
    auto = bool(replay_cfg.get("auto_pull", True))
    checks.append(
        _check(
            "repro image",
            True if present else (None if auto else False),
            "present" if present else ("missing; the first review pulls it" if auto else "missing"),
            f"docker pull {image}",
            required=False,
        )
    )
    if not (live and present and py.problem is None):
        return checks
    with tempfile.TemporaryDirectory(prefix="adrev-doctor-tree-") as tree:
        setup = replay.ReplaySetup(image=image, python=py, timeout_s=60, output_cap=65536)
        argv = replay.docker_argv(
            setup, Path(tree), ["python", "-c", "print('adrev-ok')"], f"adrev-doctor-{os.getpid()}"
        )
        try:
            out = run(argv, check=False, timeout=120)
            ok, detail = out.returncode == 0 and "adrev-ok" in out.stdout, (out.stdout + out.stderr).strip()[-300:]
        except Exception as e:  # noqa: BLE001 - report, don't crash
            ok, detail = False, str(e)[:300]
    checks.append(
        _check(
            "repro container",
            ok,
            "a --network none container ran the interpreter" if ok else detail,
            "Check that Docker can bind-mount the interpreter's directories.",
            required=False,
        )
    )
    return checks


# ---------------------------------------------------------------- live checks


def check_live(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """The real guard self-test and one probe lane, in a throwaway run outside any repository."""
    work = Path(tempfile.mkdtemp(prefix="adrev-doctor-"))
    checks: list[dict[str, Any]] = []
    try:
        fake_repo = work / "repo"
        fake_repo.mkdir()
        initial = {"repo": str(fake_repo), "round": 1, "phase": "PROBE", "mode": "no-fix", "args": {}}
        lane_run = Run.create(work / "runs", "doctor", {**initial, "escalations": [], "rounds": {"1": {}}})
        lane_run.wt_head.mkdir(parents=True)
        lane_run.wt_base.mkdir(parents=True)
        write_json(lane_run.dir / "config.json", cfg)
        ctx = Ctx(lane_run, fake_repo)
        problems = phases_lanes.guard_selftest(ctx)
        checks.append(
            _check(
                "guard self-test",
                not problems,
                "the lane guard denied and allowed exactly as expected" if not problems else "; ".join(problems)[:500],
                "Reinstall the skill; its hook scripts don't behave as shipped.",
            )
        )
        if problems:
            return checks
        probe = phases_lanes.spawn_probe(ctx, 1)
        # poll_job kills a lane 60 s after its timeout, so this wait always ends with the job done.
        deadline = time.time() + int(cfg["lanes"]["probe"]["timeout_s"]) + 90
        while lane_run.poll_job(probe["job"]) == "running" and time.time() < deadline:
            time.sleep(2)
        spec = LaneSpec.from_json(probe["spec"])
        result = lanes.collect(lane_run, spec, probe["job"])
        beats = lanes.read_heartbeat(lanes.control_dir(lane_run, spec.id) / "guard.log")
        outcome, reason = phases_lanes.probe_outcome(probe, result, beats)
        checks.append(
            _check(
                "lane probe",
                outcome == "pass",
                f"{outcome}: {reason} (${result.cost_usd:.3f})",
                "A headless `claude -p` lane could not start, authenticate or stay confined here. Check the "
                "`claude` and `auth` results above; in a cloud session, see the README's cloud notes.",
            )
        )
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return checks


def check_models(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    """One tiny call per configured model, with the lanes' environment."""
    binary = str(cfg["limits"].get("claude_bin", "claude"))
    models = sorted({lane["model"] for lane in cfg["lanes"].values() if lane.get("enabled", True)})
    checks = []
    env = {**lane_env(), "CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}
    with tempfile.TemporaryDirectory(prefix="adrev-doctor-model-") as cwd:
        for model in models:
            argv = [binary, "-p", "Reply with the single word OK.", "--model", model, "--restricted", "--tools", "Read",
                    "--permission-mode", "dontAsk", "--max-turns", "1", "--max-budget-usd", "0.50",
                    "--no-session-persistence", "--strict-mcp-config", "--output-format", "json"]  # fmt: skip
            try:
                out = run(argv, cwd=cwd, env=env, replace_env=True, check=False, timeout=180)
                data = json.loads(out.stdout) if out.stdout.strip().startswith("{") else {"result": out.stderr[-200:]}
            except Exception as e:  # noqa: BLE001 - report, don't crash
                data = {"result": str(e)}
            ok = bool(data) and not data.get("is_error") and data.get("subtype") == "success"
            detail = (
                f"ok (${float(data.get('total_cost_usd') or 0):.3f})" if ok else str(data.get("result") or "")[:200]
            )
            checks.append(
                _check(
                    f"model {model}",
                    ok,
                    detail,
                    "Use a model your plan can access for this lane in review.config.json (lanes.<name>.model).",
                )
            )
    return checks


# ---------------------------------------------------------------- entry


def run_doctor(start: Path, offline: bool = False, models: bool = False) -> dict[str, Any]:
    checks = [check_python(), check_git()]
    repo_checks, repo, cfg = check_repository(start)
    checks += repo_checks
    checks.append(check_paths(repo))
    claude_checks, claude_ok = check_claude(cfg)
    checks += claude_checks
    checks.append(check_read_scope(repo))
    checks += check_replay(cfg, live=not offline)
    if offline:
        checks.append(_check("lane probe", None, "skipped (--offline)"))
    elif claude_ok:
        checks += check_live(cfg)
    else:
        checks.append(
            _check(
                "lane probe", False, "skipped: `claude` is missing, too old or not logged in", "Fix the checks above."
            )
        )
    if models and claude_ok and not offline:
        checks += check_models(cfg)
    ready = all(c["ok"] is not False for c in checks if c["required"])
    return {"version": VERSION, "skill_dir": str(SKILL_DIR), "ready": ready, "checks": checks}
