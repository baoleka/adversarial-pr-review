"""Review driver: owns the state machine; the agent only follows the actions it prints.

    review.py start [--no-fix] [--ack FINDING_ID]... [--base-ref REF] [--repo PATH] [INTENT...]
    review.py next  [--run RUN_DIR]
    review.py do ACTION_ID [--run RUN_DIR]
    review.py wait ACTION_ID [--run RUN_DIR] [--max-seconds 540]
    review.py ingest ACTION_ID [--run RUN_DIR] [--answer TEXT] [--dispositions FILE]
    review.py dispose FINDING_ID --action fix|dispute|defer --note TEXT [...] [--run RUN_DIR]
    review.py checklist-add TEXT [--run RUN_DIR]
    review.py show [--verdict | --report | --findings | --author] [--run RUN_DIR]
    review.py doctor [--offline] [--models] [--repo PATH]

Every command prints one JSON object: {"run": ..., "action": {...}}. The agent runs the action's
argv exactly, waits with `wait`, fixes when asked (`author`), asks the user (`ask`), and stops
at `done` or `escalate`, reporting the verdict from verdict.json. `doctor` prints a setup report.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Any

# The skill may be installed in a directory Claude Code manages (a plugin cache): don't write
# __pycache__ there. Set before the skill's own modules are imported.
sys.dont_write_bytecode = True

import phases_lanes  # noqa: F401 - registers BRIEFS, PROBE, LANES and NORMALIZE
import phases_loop  # noqa: F401 - registers FIXREPLAY, FIXCHECK and ADJUDICATE
import phases_mech  # noqa: F401 - registers TRIAGE, GATES and CHECKS
import phases_setup  # noqa: F401 - registers INIT and SNAPSHOT
import phases_verify  # noqa: F401 - registers REPLAY, VERIFY, SYNTHESIZE and VERDICT
from engine import PHASES, Ctx, next_phase, phase_order

import config as config_mod
import ledger as ledger_mod
import snapshot as snap
from common import SCRIPTS_DIR, VERSION, ReviewError, canonical_json, read_json
from state import Run, TamperError, new_run_id

MAX_DO_SECONDS = 90
WAIT_SECONDS = 540
WAIT_POLL = 2.0
STUCK_LIMIT = 3

# ---------------------------------------------------------------- engine


def driver_path() -> str:
    """The path this driver was invoked by, not resolved through symlinks, so the commands it
    prints keep matching the skill's `allowed-tools` rule when the install dir is reached through
    a symlink (a symlinked ~/.claude or plugin cache). Falls back to the real path."""
    real = SCRIPTS_DIR / "review.py"
    invoked = sys.argv[0] if sys.argv else ""
    if invoked and Path(invoked).name == "review.py":
        candidate = os.path.abspath(invoked)
        try:
            if Path(candidate).resolve() == real.resolve():
                return candidate
        except OSError:
            pass
    return str(real)


def self_argv(*args: str) -> list[str]:
    # `python3` (not sys.executable) so the command matches the skill's allowlist entry
    # `Bash(python3 -E -s <skill>/scripts/review.py *)`.
    return ["python3", "-E", "-s", driver_path(), *args]


def make_action(ctx: Ctx, kind: str, **extra: Any) -> dict[str, Any]:
    """Create (or re-issue) the action for the current position; detect stuck loops."""
    st = ctx.state
    key = f"r{ctx.round}:{st['phase']}:{kind}"
    actions = st["actions"]
    current_hash = ctx.run.state_hash()
    if actions and actions[-1]["key"] == key and actions[-1]["status"] == "pending":
        action = actions[-1]
        if action["state_hash"] == current_hash:
            action["issued"] += 1
        else:
            action["state_hash"] = current_hash
    else:
        action = {
            "id": f"a{ctx.run.next_seq():04d}",
            "key": key,
            "kind": kind,
            "phase": st["phase"],
            "round": ctx.round,
            "issued": 1,
            "state_hash": current_hash,
            "status": "pending",
        }
        actions.append(action)
    waiting_ok = kind == "wait" and any(ctx.run.poll_job(j) == "running" for j in st.get("waiting_on", []))
    if action["issued"] >= STUCK_LIMIT and not waiting_ok and kind not in ("done", "escalate"):
        ctx.escalate(
            "stuck-action", f"action {action['id']} ({key}) was issued {action['issued']} times with no state change"
        )
        return finish(ctx, "ESCALATE")
    out = {"id": action["id"], "kind": kind, **extra}
    if kind == "exec":
        out["argv"] = self_argv("do", action["id"], "--run", str(ctx.run.dir))
    elif kind == "wait":
        out["argv"] = self_argv("wait", action["id"], "--run", str(ctx.run.dir))
        out["timeout_ms"] = (WAIT_SECONDS + 30) * 1000
    elif kind == "author":
        run_dir = str(ctx.run.dir)
        out["dispose"] = (
            shlex.join(self_argv("dispose", "--run", run_dir))
            + " FINDING_ID --action fix|dispute|defer --note 'what you changed, or why it is wrong'"
            + " [--cite head:path/to/file.py:LINE ...] [--diverges --reason 'why your fix differs']"
        )
        out["continue"] = shlex.join(self_argv("ingest", action["id"], "--run", run_dir))
    elif kind == "ask":
        out["ingest"] = (
            shlex.join(self_argv("ingest", action["id"], "--run", str(ctx.run.dir))) + " --answer 'THE ANSWER'"
        )
    if "argv" in out:
        out["command"] = shlex.join(out["argv"])
    return out


def finish(ctx: Ctx, verdict: str, minimal: bool = False) -> dict[str, Any]:
    """End the run. ``minimal`` writes a bare ESCALATE verdict (used when the verdict phase itself failed)."""
    st = ctx.state
    st["terminal"] = verdict
    st["phase"] = "DONE"
    if minimal or not (ctx.run.dir / "verdict.json").is_file():
        ctx.run.write_state_file(
            "verdict.json",
            {
                "verdict": verdict,
                "reason": "; ".join(e["detail"] for e in st.get("escalations", [])),
                "escalations": st.get("escalations", []),
                "run_id": st["run_id"],
                "round": ctx.round,
            },
        )
    kind = "done" if verdict in ("APPROVE", "REQUEST_CHANGES") else "escalate"
    for action in st["actions"]:
        if action["status"] == "pending":
            action["status"] = "done"
    apply_retention(ctx)
    return {
        "id": "final",
        "kind": kind,
        "verdict": verdict,
        "argv": self_argv("show", "--summary", "--run", str(ctx.run.dir)),
        "command": shlex.join(self_argv("show", "--summary", "--run", str(ctx.run.dir))),
    }


def apply_retention(ctx: Ctx) -> None:
    """After the verdict: drop the gates' caches and (unless `retention.keep_worktrees`) the run's
    worktrees, and remove finished runs beyond `retention.keep_runs`. Reports, findings, verdicts and
    the snapshot refs stay."""
    if ctx.state.get("retention_done"):
        return
    cfg_path = ctx.run.dir / "config.json"
    limits = read_json(cfg_path)["limits"] if cfg_path.is_file() else config_mod.DEFAULTS["limits"]
    retention = limits.get("retention", {})
    shutil.rmtree(ctx.run.dir / "cache", ignore_errors=True)
    if not retention.get("keep_worktrees", False):
        for worktree in (ctx.run.wt_head, ctx.run.wt_base):
            if worktree.exists():
                snap.remove_worktree(ctx.repo, worktree)
    finished = []
    for other in sorted(ctx.run.dir.parent.iterdir()):
        if other == ctx.run.dir or not (other / "state.json").is_file():
            continue
        try:
            if read_json(other / "state.json").get("terminal"):
                finished.append(other)
        except (OSError, ValueError):
            continue
    for old in finished[: max(0, len(finished) - (int(retention.get("keep_runs", 5)) - 1))]:
        for worktree in (old / "wt" / "head", old / "wt" / "base"):
            if worktree.exists():
                snap.remove_worktree(ctx.repo, worktree)
        shutil.rmtree(old, ignore_errors=True)
    ctx.state["retention_done"] = True


def drive(ctx: Ctx, deadline: float) -> dict[str, Any]:
    """Run synchronous phases until one needs the agent, a wait, or the deadline."""
    st = ctx.state
    while True:
        if st.get("terminal"):
            return finish(ctx, st["terminal"])
        if st.get("escalations") and st["phase"] not in ("VERDICT", "DONE"):
            # Fail closed: an ESCALATE condition ends the round at the verdict phase.
            st["phase"] = "VERDICT"
        name = st["phase"]
        handler = PHASES[name]
        st["in_progress"] = {
            "phase": name,
            "round": ctx.round,
            "attempts": st.get("in_progress", {}).get("attempts", 0) + 1
            if st.get("in_progress", {}).get("phase") == name
            else 1,
        }
        ctx.run.save()
        if st["in_progress"]["attempts"] > 2:
            ctx.escalate("degraded", f"phase {name} failed twice")
            st["phase"] = "VERDICT"
            continue
        try:
            step = handler(ctx)
        except Exception as e:  # noqa: BLE001 - any failure fails closed, with the traceback logged
            if not isinstance(e, ReviewError):
                ctx.run.event("internal-error", phase=name, traceback=traceback.format_exc()[-4000:])
            detail = str(e) if isinstance(e, ReviewError) else f"internal error {type(e).__name__}: {e}"
            ctx.escalate("degraded", f"{name}: {detail}")
            st.pop("in_progress", None)
            if name == "VERDICT":
                # The verdict phase itself failed: don't loop back into it.
                return finish(ctx, "ESCALATE", minimal=True)
            st["phase"] = "VERDICT"
            continue
        st.pop("in_progress", None)
        if step.kind == "terminal":
            return finish(ctx, step.payload["verdict"])
        if step.kind == "wait":
            st["waiting_on"] = step.jobs
            st["wait_any"] = bool(step.payload.get("any_done"))
            return make_action(ctx, "wait", say=f"{len(step.jobs)} background job(s) running for {name}")
        if step.kind in ("author", "ask"):
            st["last_action_payload"] = step.payload
            return make_action(ctx, step.kind, **step.payload)
        st["phase"] = next_phase(ctx) if name != "INIT" else phase_order(ctx.round)[0]
        if time.time() > deadline:
            return make_action(ctx, "exec", say=f"continue at {st['phase']}")


# ---------------------------------------------------------------- commands


def emit(run: Run, action: dict[str, Any]) -> int:
    run.save()
    print(json.dumps({"run": str(run.dir), "action": action}, indent=2, sort_keys=True))
    return 0


def find_run(args: argparse.Namespace) -> tuple[Run, Path]:
    if args.run:
        run_dir = Path(args.run).resolve()
    else:
        repo = snap.repo_root(Path.cwd())
        found = Run.current(repo / ".reviews")
        if found is None:
            raise ReviewError("no active review run; start one with `review.py start`")
        run_dir = found
    run = Run.load(run_dir)
    started_with = run.state.get("driver_version", VERSION)
    if started_with != VERSION:
        raise ReviewError(
            f"this review was started by adversarial-review {started_with}, but this is {VERSION} "
            "(the skill was updated mid-run); start a new review"
        )
    return run, Path(run.state["repo"])


def cmd_start(args: argparse.Namespace) -> int:
    repo = snap.repo_root(Path(args.repo or Path.cwd()))
    run_dir_rel = args.run_dir or config_mod.DEFAULTS["limits"]["run_dir"]
    run_root = repo / run_dir_rel
    run_root.mkdir(parents=True, exist_ok=True)
    snap.ensure_excluded(repo, run_dir_rel)
    run_id = new_run_id()
    intent = " ".join(args.intent).strip()
    run = Run.create(
        run_root,
        run_id,
        {
            "repo": str(repo),
            "driver_version": VERSION,
            "run_dir_rel": run_dir_rel,
            "mode": "no-fix" if args.no_fix else "loop",
            "args": {"base_ref": args.base_ref, "ack": sorted(set(args.ack or [])), "budget_usd": args.budget},
            "intent": intent or None,
            "intent_source": "argument" if intent else None,
            "round": 1,
            "phase": "INIT",
            "escalations": [],
            "rounds": {},
        },
    )
    run.write_state_file("ledger.json", ledger_mod.empty(run_id))
    run.event("start", repo=str(repo), mode=run.state["mode"])
    ctx = Ctx(run, repo)
    return emit(run, drive(ctx, time.time() + MAX_DO_SECONDS))


def cmd_next(args: argparse.Namespace) -> int:
    run, repo = find_run(args)
    ctx = Ctx(run, repo)
    if run.state.get("terminal"):
        return emit(run, finish(ctx, run.state["terminal"]))
    last = run.state["actions"][-1] if run.state["actions"] else None
    if last and last["status"] == "pending" and last["kind"] in ("author", "ask", "wait"):
        extra = run.state.get("last_action_payload", {})
        return emit(run, make_action(ctx, last["kind"], **extra))
    return emit(run, make_action(ctx, "exec", say=f"continue at {run.state['phase']}"))


def _resolve_pending(run: Run, action_id: str, kind: str) -> None:
    pending = [a for a in run.state["actions"] if a["id"] == action_id]
    if not pending:
        raise ReviewError(f"unknown action {action_id}")
    if pending[0]["kind"] != kind:
        raise ReviewError(f"action {action_id} is a {pending[0]['kind']} action, not {kind}")
    pending[0]["status"] = "done"


def cmd_do(args: argparse.Namespace) -> int:
    run, repo = find_run(args)
    ctx = Ctx(run, repo)
    if run.state.get("terminal"):
        return emit(run, finish(ctx, run.state["terminal"]))
    _resolve_pending(run, args.action_id, "exec")
    return emit(run, drive(ctx, time.time() + MAX_DO_SECONDS))


def cmd_wait(args: argparse.Namespace) -> int:
    run, repo = find_run(args)
    ctx = Ctx(run, repo)
    if run.state.get("terminal"):
        return emit(run, finish(ctx, run.state["terminal"]))
    deadline = time.time() + min(args.max_seconds, WAIT_SECONDS)
    waiting = list(run.state.get("waiting_on", []))
    wait_any = bool(run.state.get("wait_any"))

    def still_waiting() -> bool:
        running = [run.poll_job(j) == "running" for j in waiting]
        return all(running) if wait_any and running else any(running)

    while time.time() < deadline and still_waiting():
        run.save()
        time.sleep(WAIT_POLL)
    if still_waiting():
        return emit(run, make_action(ctx, "wait", say="jobs still running"))
    _resolve_pending(run, args.action_id, "wait")
    run.state["waiting_on"] = []
    return emit(run, drive(ctx, time.time() + MAX_DO_SECONDS))


def cmd_ingest(args: argparse.Namespace) -> int:
    run, repo = find_run(args)
    ctx = Ctx(run, repo)
    pending = [a for a in run.state["actions"] if a["id"] == args.action_id]
    if not pending:
        raise ReviewError(f"unknown action {args.action_id}")
    kind = pending[0]["kind"]
    if kind == "ask":
        if not args.answer:
            raise ReviewError("this action needs --answer")
        key = run.state.pop("pending_ask", None)
        if key == "base_ref":
            if not snap.resolve_base_ref(repo, args.answer, run.state.get("remote", "origin")) == args.answer:
                raise ReviewError(f"{args.answer} does not resolve to a commit")
            run.state["base_ref"] = args.answer
    elif kind == "author":
        import dispositions

        dispositions.ingest(ctx, args.dispositions)
    else:
        raise ReviewError(f"action {args.action_id} is a {kind} action; use `do` or `wait`")
    _resolve_pending(run, args.action_id, kind)
    return emit(run, drive(ctx, time.time() + MAX_DO_SECONDS))


def cmd_dispose(args: argparse.Namespace) -> int:
    import dispositions

    run, _repo = find_run(args)
    ctx = Ctx(run, _repo)
    last = run.state["actions"][-1] if run.state["actions"] else None
    if not last or last["kind"] != "author" or last["status"] != "pending":
        raise ReviewError("no author step is open; run `review.py next`")
    found = dispositions.current_findings(read_json(run.dir / "findings.json"), ctx.round)
    pending = run.state["rounds"][str(ctx.round)].setdefault("dispositions", {})
    recorded = dispositions.record(
        found,
        pending,
        args.finding_id,
        args.action,
        args.note,
        args.diverges,
        args.reason,
        args.cite or [],
        {"head": run.wt_head, "base": run.wt_base},
    )
    run.event("dispose", finding=args.finding_id, action=args.action)
    run.save()
    remaining = dispositions.missing(found, pending)
    print(
        json.dumps(
            {
                "run": str(run.dir),
                "recorded": {args.finding_id: recorded},
                "remaining": remaining,
                "next": "dispose the remaining findings" if remaining else "run the author action's continue command",
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    run, _ = find_run(args)
    if args.report:
        path = run.dir / "report.md"
        print(path.read_text(encoding="utf-8") if path.is_file() else "(no report yet)")
    elif args.findings:
        path = run.dir / "findings.json"
        print(path.read_text(encoding="utf-8") if path.is_file() else "{}")
    elif args.author:
        path = run.round_dir() / "author.md"
        print(path.read_text(encoding="utf-8") if path.is_file() else "(nothing to address)")
    else:
        verdict_path = run.dir / "verdict.json"
        verdict = read_json(verdict_path) if verdict_path.is_file() else {"verdict": None}
        summary = {
            "verdict": verdict.get("verdict"),
            "reason": verdict.get("reason"),
            "round": run.state.get("round"),
            "phase": run.state.get("phase"),
            "report": str(run.dir / "report.md"),
            "escalations": run.state.get("escalations", []),
        }
        print(canonical_json(summary))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    import doctor

    report = doctor.run_doctor(Path(args.repo or Path.cwd()), offline=args.offline, models=args.models)
    print(canonical_json(report))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="review.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("start")
    p.add_argument("--no-fix", action="store_true")
    p.add_argument("--ack", action="append", metavar="FINDING_ID")
    p.add_argument("--base-ref")
    p.add_argument("--repo")
    p.add_argument("--run-dir", help=argparse.SUPPRESS)
    p.add_argument("--budget", type=float)
    p.add_argument("intent", nargs=argparse.REMAINDER)
    p.set_defaults(func=cmd_start)

    for name, func in (("next", cmd_next), ("show", cmd_show)):
        p = sub.add_parser(name)
        p.add_argument("--run")
        if name == "show":
            group = p.add_mutually_exclusive_group()
            group.add_argument("--verdict", action="store_true")
            group.add_argument("--report", action="store_true")
            group.add_argument("--findings", action="store_true")
            group.add_argument("--author", action="store_true")
            group.add_argument("--summary", action="store_true")
        p.set_defaults(func=func)

    p = sub.add_parser("do")
    p.add_argument("action_id")
    p.add_argument("--run")
    p.set_defaults(func=cmd_do)

    p = sub.add_parser("wait")
    p.add_argument("action_id")
    p.add_argument("--run")
    p.add_argument("--max-seconds", type=float, default=WAIT_SECONDS)
    p.set_defaults(func=cmd_wait)

    p = sub.add_parser("dispose")
    p.add_argument("finding_id")
    p.add_argument("--action", required=True, choices=("fix", "dispute", "defer"))
    p.add_argument("--note", required=True)
    p.add_argument("--cite", action="append", metavar="SIDE:FILE:LINE")
    p.add_argument("--diverges", action="store_true")
    p.add_argument("--reason")
    p.add_argument("--run")
    p.set_defaults(func=cmd_dispose)

    p = sub.add_parser("ingest")
    p.add_argument("action_id")
    p.add_argument("--run")
    p.add_argument("--answer")
    p.add_argument("--dispositions")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("doctor", help="check this machine's setup and print a JSON report with fixes")
    p.add_argument("--offline", action="store_true", help="skip the live checks (no model call, no container)")
    p.add_argument("--models", action="store_true", help="also check access to every configured model")
    p.add_argument("--repo")
    p.set_defaults(func=cmd_doctor)

    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except TamperError as e:
        print(
            json.dumps(
                {"action": {"id": "final", "kind": "escalate", "verdict": "ESCALATE", "reason": f"integrity: {e}"}},
                indent=2,
            )
        )
        return 0
    except ReviewError as e:
        print(json.dumps({"error": str(e)}, indent=2), file=sys.stderr)
        return 2
    except Exception:  # noqa: BLE001 - fail closed with the traceback on stderr
        traceback.print_exc()
        return 3


if __name__ == "__main__":
    sys.exit(main())
