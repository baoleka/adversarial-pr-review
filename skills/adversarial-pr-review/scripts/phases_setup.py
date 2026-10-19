"""Run setup (INIT) and the per-round snapshot and worktrees (SNAPSHOT)."""

from __future__ import annotations

from pathlib import Path

from engine import Ctx, Step, phase

import config as config_mod
import snapshot as snap
from common import git, write_json

# ---------------------------------------------------------------- run-level setup (INIT)


def run_root_for(repo: Path, run_dir_rel: str) -> Path:
    return repo / run_dir_rel


def draft_intent(repo: Path, merge_base: str, snapshot_tree: str) -> str:
    log = git(repo, "log", "--reverse", "--format=%s%n%n%b", f"{merge_base}..HEAD", check=False).strip()
    head_tree = git(repo, "rev-parse", "HEAD^{tree}", check=False).strip()
    parts = [f"Commit messages:\n{log}" if log else "No commits on this branch yet."]
    if head_tree != snapshot_tree:
        parts.append("The working tree also has uncommitted changes.")
    return "\n\n".join(parts)


@phase("INIT")
def init_phase(ctx: Ctx) -> Step:
    """Resolve the base ref, merge-base and config (read from the merge-base); freeze the intent."""
    st = ctx.state
    repo = ctx.repo
    run_dir_rel = st["run_dir_rel"]
    snap.ensure_excluded(repo, run_dir_rel)
    if "snapshot" not in st.setdefault("rounds", {}).setdefault("1", {}):
        cap = int(config_mod.DEFAULTS["limits"]["untracked_size_cap"])
        shot = snap.take_snapshot(repo, ctx.run.dir / "tmp", run_dir_rel, st["run_id"], 1, cap)
        st["rounds"]["1"]["snapshot"] = shot.__dict__
    snapshot_commit = st["rounds"]["1"]["snapshot"]["commit"]

    explicit = st["args"].get("base_ref")
    base_ref = st.get("base_ref") or explicit or snap.resolve_base_ref(repo, None, "origin")
    shallow = snap.is_shallow(repo)
    if base_ref is None:
        st["pending_ask"] = "base_ref"
        hint = (
            " This clone may lack the base branch: fetch it first (for example "
            "`git fetch origin main:refs/remotes/origin/main`) and start again, or name a ref."
            if shallow
            else ""
        )
        return Step.ask(
            "No base branch found (tried origin/HEAD, origin/main and origin/master). "
            f"Which ref should the change be compared against?{hint}",
            "base_ref",
        )
    seen: list[str] = []
    while True:
        bases = snap.merge_bases(repo, base_ref, snapshot_commit)
        if not bases and shallow:
            ctx.escalate(
                "shallow-clone",
                f"this is a shallow clone, so the history to {base_ref} that the review needs is missing",
                decision="Fetch the full history (`git fetch --unshallow origin`, or `git fetch --deepen=500 "
                "origin`), then start the review again.",
            )
            return Step.terminal("ESCALATE")
        if len(bases) != 1:
            ctx.escalate(
                "merge-base",
                f"git merge-base --all {base_ref} HEAD returned {len(bases)} commits",
                decision="Pick a base ref with a single merge-base, or rebase the branch.",
            )
            return Step.terminal("ESCALATE")
        merge_base = bases[0]
        loaded = config_mod.load(repo, merge_base, head_root=repo)
        remote = loaded.data["repo"]["remote"]
        if explicit:
            break
        # The base ref order is: config, <remote>/HEAD, <remote>/main, <remote>/master. The
        # config is read from the merge-base, so re-check once it is known.
        preferred = snap.resolve_base_ref(repo, loaded.data["repo"]["base_ref"], remote)
        if preferred is None or preferred == base_ref:
            break
        seen.append(base_ref)
        if preferred in seen or len(seen) > 2:
            ctx.escalate(
                "merge-base",
                "the configured base ref keeps changing the merge-base",
                decision="Set base_ref in review.config.json or pass --base-ref.",
            )
            return Step.terminal("ESCALATE")
        base_ref = preferred

    st["base_ref"] = base_ref
    st["remote"] = remote
    st["merge_base"] = merge_base
    st["config_source"] = {"source": loaded.source, "path": loaded.path, "sha256": loaded.sha256}
    write_json(ctx.run.dir / "config.json", loaded.data)
    stale = snap.stale_base_warning(repo, base_ref, remote, int(loaded.data["limits"]["stale_fetch_age_hours"]))
    if shallow:
        stale = "; ".join(filter(None, [stale, "shallow clone (history before the clone depth is missing)"]))
    st["stale_base"] = stale or None
    if loaded.data["limits"]["run_dir"] != run_dir_rel:
        ctx.run.event("note", detail="config run_dir differs from the run dir in use; keeping the current run dir")
    if not st.get("intent"):
        st["intent"] = draft_intent(repo, merge_base, st["rounds"]["1"]["snapshot"]["tree"])
        st["intent_source"] = "drafted"
    return Step.advance()


# ---------------------------------------------------------------- per-round snapshot


@phase("SNAPSHOT")
def snapshot_phase(ctx: Ctx) -> Step:
    st = ctx.state
    cfg = ctx.config()
    rnd = st["rounds"].setdefault(str(ctx.round), {})
    if "snapshot" not in rnd:
        shot = snap.take_snapshot(
            ctx.repo,
            ctx.run.dir / "tmp",
            st["run_dir_rel"],
            st["run_id"],
            ctx.round,
            int(cfg["limits"]["untracked_size_cap"]),
        )
        rnd["snapshot"] = shot.__dict__
        bases = snap.merge_bases(ctx.repo, st["base_ref"], shot.commit)
        if bases != [st["merge_base"]]:
            ctx.escalate(
                "merge-base",
                "the merge-base changed between rounds (was the branch rebased?)",
                decision="Restart the review on the rebased branch.",
            )
            return Step.terminal("ESCALATE")
    shot_commit = rnd["snapshot"]["commit"]
    for path, sha in ((ctx.run.wt_base, st["merge_base"]), (ctx.run.wt_head, shot_commit)):
        snap.add_worktree(ctx.repo, path, sha, ctx.run.no_hooks)
    rnd["author_tree_before"] = rnd["snapshot"]["tree"]
    return Step.advance()
