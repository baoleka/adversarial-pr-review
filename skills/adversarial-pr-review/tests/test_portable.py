"""Portability fixes made for the plugin build: a hardened guard, symlink-safe driver commands, a
per-run version pin, an ingest command on `ask`, shallow clones, the replay image pull, and doctor."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any
from unittest import mock

from support import SCRIPTS, FixtureRepo

# isort: split

import phases_lanes
import phases_mech
import replay
import review
from engine import Ctx
from test_m3_lanes import BASE_SRC, HEAD_SRC, REVIEW, TmpCase

import snapshot as snap
from common import ReviewError
from state import Run

sys.path.insert(0, str(SCRIPTS / "hooks"))
import guard  # noqa: E402

GUARD = [sys.executable, "-E", "-s", str(SCRIPTS / "hooks" / "guard.py")]


def run_review(cwd: Path, *args: str, env: dict[str, str] | None = None) -> dict[str, Any]:
    proc = subprocess.run([*REVIEW, *args], cwd=cwd, capture_output=True, text=True, env=env, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"review.py {args[:2]} failed: {proc.stdout}{proc.stderr}")
    out: dict[str, Any] = json.loads(proc.stdout)
    return out


class GuardHardeningTest(TmpCase):
    def test_shell_hazards_respect_quoting(self) -> None:
        driver = "python3 -E -s /x/review.py"
        cases = {
            f"{driver} next": None,
            f"{driver} dispose F1 --note 'uses `foo` > bar; and | more'": None,
            f'{driver} dispose F1 --note "a \\`b\\` > c; d"': None,
            f"{driver} next; rm -rf .reviews": ";",
            f"{driver} next && rm x": "&",
            f'{driver} dispose F1 --note "$(rm x)"': "$(",
            f'{driver} dispose F1 --note "`rm x`"': "`",
            f"{driver} next > .reviews/x": ">",
            f"{driver} next\nrm x": "\n",
            f"{driver} dispose F1 --note 'open": "an unterminated quote",
        }
        self.assertEqual({c: guard.shell_hazard(c) for c in cases}, cases)

    def test_author_mode_closes_the_chained_command_bypass(self) -> None:
        driver = f"python3 -E -s {SCRIPTS / 'review.py'}"
        run_dir = "/r/.reviews/20260101-000000-abcd"
        commands = [
            f"{driver} do a0001 --run {run_dir}",
            f"{driver} dispose F-1 --run {run_dir} --note 'dropped the rm call; see `cleanup()`'",
            f"{driver} next --run {run_dir}; rm {run_dir}/state.json",
            f"rm {run_dir}/state.json",
            f"cat {run_dir}/report.md",
        ]
        decisions = [
            guard.decide_author({"tool_name": "Bash", "tool_input": {"command": c}, "cwd": "/r"}, str(SCRIPTS))[0]
            for c in commands
        ]
        self.assertEqual(decisions, [True, True, False, False, True])

    def test_lane_mode_only_for_the_skills_own_agents(self) -> None:
        env = {k: v for k, v in os.environ.items() if k not in ("ADREV_LANE_ID", "ADREV_LANE_POLICY")}

        def exit_code(agent_type: str) -> int:
            event = {
                "tool_name": "Write",
                "tool_input": {"file_path": str(self.tmp / "x.txt"), "content": ""},
                "cwd": str(self.tmp),
                "agent_type": agent_type,
            }
            proc = subprocess.run(GUARD, input=json.dumps(event), env=env, capture_output=True, text=True, check=False)
            return proc.returncode

        # Lane mode without a lane policy denies (exit 2); author mode allows an ordinary write.
        self.assertEqual(
            {a: exit_code(a) for a in ("review-breaker", "adrev:review-breaker", "review-custom", "general-purpose")},
            {"review-breaker": 2, "adrev:review-breaker": 2, "review-custom": 0, "general-purpose": 0},
        )


class DriverCommandsTest(TmpCase):
    def docs_change_repo(self) -> FixtureRepo:
        repo = FixtureRepo.create(self.tmp, {"README.md": "old\n", "pkg/__init__.py": ""})
        repo.write("README.md", "new\n")
        repo.commit("docs")
        return repo

    def test_commands_keep_the_invoked_path_through_a_symlink(self) -> None:
        link = self.tmp / "skill-link"
        os.symlink(SCRIPTS.parent, link)
        invoked = str(link / "scripts" / "review.py")
        with mock.patch.object(sys, "argv", [invoked]):
            via_link = review.driver_path()
        with mock.patch.object(sys, "argv", ["/elsewhere/run_tests.py"]):
            fallback = review.driver_path()
        repo = self.docs_change_repo()
        proc = subprocess.run(
            [sys.executable, "-E", "-s", invoked, "start", "--no-fix", "docs"],
            cwd=repo.root,
            capture_output=True,
            text=True,
            check=True,
        )
        printed = json.loads(proc.stdout)["action"]["argv"][3]
        self.assertEqual((via_link, fallback, printed), (invoked, str(SCRIPTS / "review.py"), invoked))

    def test_a_run_started_by_another_version_is_refused(self) -> None:
        repo = self.docs_change_repo()
        out = run_review(repo.root, "start", "--no-fix", "docs")
        args = argparse.Namespace(run=out["run"])
        same, _ = review.find_run(args)
        with mock.patch.object(review, "VERSION", "9.9.9"), self.assertRaises(ReviewError) as caught:
            review.find_run(args)
        self.assertEqual(
            (same.state["driver_version"], "updated mid-run" in str(caught.exception)),
            (review.VERSION, True),
        )

    def test_ask_actions_carry_the_ingest_command(self) -> None:
        repo = self.docs_change_repo()
        repo.git("symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        repo.git("update-ref", "-d", "refs/remotes/origin/master")
        out = run_review(repo.root, "start", "--no-fix", "x")
        action = out["action"]
        expected = shlex.join(
            ["python3", "-E", "-s", str(SCRIPTS / "review.py"), "ingest", action["id"], "--run", out["run"]]
        )
        self.assertEqual((action["kind"], action["ingest"]), ("ask", expected + " --answer 'THE ANSWER'"))


class ShallowCloneTest(TmpCase):
    def test_a_shallow_clone_without_the_merge_base_escalates_with_the_fetch_command(self) -> None:
        src = FixtureRepo.create(self.tmp, {"pkg/ranges.py": BASE_SRC, "pkg/__init__.py": ""}, name="src")
        src.write("pkg/ranges.py", HEAD_SRC)
        src.commit("change clamp")
        clone = self.tmp / "clone"
        url = f"file://{src.root}"
        subprocess.run(["git", "clone", "-q", "--depth", "1", "--branch", "feature", url, str(clone)], check=True)
        subprocess.run(
            ["git", "-C", str(clone), "fetch", "-q", "--depth", "1", "origin", "master:refs/remotes/origin/master"],
            check=True,
        )
        # The fixture's origin has `feature` checked out; a hosted remote's HEAD is its default branch.
        subprocess.run(["git", "-C", str(clone), "remote", "set-head", "origin", "-d"], check=False)
        out = run_review(clone, "start", "--no-fix", "change clamp")
        verdict = json.loads((Path(out["run"]) / "verdict.json").read_text())
        conditions = [e["condition"] for e in verdict["escalations"]]
        decision = verdict["escalations"][0]["decision_needed"]
        self.assertEqual(
            (snap.is_shallow(clone), snap.is_shallow(src.root), out["action"]["kind"], conditions, "--unshallow" in decision),
            (True, False, "escalate", ["shallow-clone"], True),
        )  # fmt: skip


class ImagePullTest(TmpCase):
    def test_pull_only_when_replays_could_run_but_the_image_is_missing(self) -> None:
        base = {"isolation": "auto", "auto_pull": True, "image": "img@sha256:0"}

        def needed(cfg: dict[str, Any], daemon: str | None, present: bool, py_problem: str | None) -> bool:
            with (
                mock.patch.object(replay, "docker_daemon_problem", return_value=daemon),
                mock.patch.object(replay, "image_present", return_value=present),
                mock.patch.object(replay.sys, "platform", "linux"),
            ):
                return replay.pull_needed(cfg, py_problem)

        self.assertEqual(
            [
                needed(base, None, False, None),
                needed(base, None, True, None),
                needed(base, "no daemon", False, None),
                needed(base, None, False, "system interpreter"),
                needed({**base, "auto_pull": False}, None, False, None),
                needed({**base, "isolation": "none"}, None, False, None),
            ],
            [True, False, False, False, False, False],
        )

    def test_gates_start_the_pull_and_briefs_waits_for_it(self) -> None:
        repo = FixtureRepo.create(self.tmp, {"README.md": "x\n"})
        run = Run.create(repo.root / ".reviews", "t1", {"repo": str(repo.root), "round": 1, "phase": "GATES"})
        ctx = Ctx(run, repo.root)
        cfg = {"replay": {"isolation": "auto", "auto_pull": True, "image": "img@sha256:0"}}
        with (
            mock.patch.object(replay, "pull_needed", return_value=True),
            mock.patch.object(replay, "pull_argv", return_value=[sys.executable, "-c", "import time; time.sleep(30)"]),
        ):
            phases_mech.maybe_pull_image(ctx, cfg)
            phases_mech.maybe_pull_image(ctx, cfg)  # once per run
        job = run.state["image_pull"]["job"]
        step = phases_lanes.briefs_phase(ctx)
        os.killpg(os.getpgid(int(run.state["jobs"][job]["pid"])), 9)
        self.assertEqual(
            (len(run.state["jobs"]), run.state["jobs"][job]["kind"], step.kind, step.jobs), (1, "pull", "wait", [job])
        )

    def test_repros_need_a_linux_host(self) -> None:
        with mock.patch.object(replay.sys, "platform", "darwin"):
            py = replay.describe_python()
        self.assertEqual("Linux host" in (py.problem or ""), True)


class DoctorTest(TmpCase):
    def setUp(self) -> None:
        super().setUp()
        self.config_dir = self.tmp / "claude-config"
        self.config_dir.mkdir()
        os.environ["CLAUDE_CONFIG_DIR"] = str(self.config_dir)
        for key in ("FAKE_CLAUDE_PLAN", "FAKE_CLAUDE_SCENARIO", "FAKE_CLAUDE_LOGGED_IN", "CLAUDE_CODE_REMOTE"):
            os.environ.pop(key, None)
        self.repo = FixtureRepo.create(self.tmp, {"pkg/ranges.py": BASE_SRC, "pkg/__init__.py": ""})
        self.repo.write("pkg/ranges.py", HEAD_SRC)
        self.repo.commit("change clamp")

    def doctor(self, *args: str) -> tuple[dict[str, Any], str]:
        proc = subprocess.run(
            [*REVIEW, "doctor", *args], cwd=self.repo.root, capture_output=True, text=True, check=True
        )
        return json.loads(proc.stdout), proc.stdout

    @staticmethod
    def by_name(report: dict[str, Any]) -> dict[str, Any]:
        return {c["name"]: c["ok"] for c in report["checks"]}

    def test_offline_report(self) -> None:
        report, raw = self.doctor("--offline")
        checks = self.by_name(report)
        self.assertEqual(
            (
                report["ready"],
                report["version"],
                {k: checks[k] for k in ("python", "git", "repository", "base ref", "merge-base", "config", "paths")},
                {k: checks[k] for k in ("claude", "auth", "read scope", "lane probe")},
                "someone@example.invalid" in raw,
            ),
            (
                True,
                review.VERSION,
                {k: True for k in ("python", "git", "repository", "base ref", "merge-base", "config", "paths")},
                {"claude": True, "auth": True, "read scope": True, "lane probe": None},
                False,
            ),
        )

    def test_live_probe_through_the_fake_claude(self) -> None:
        report, _ = self.doctor()
        checks = self.by_name(report)
        self.assertEqual((report["ready"], checks["guard self-test"], checks["lane probe"]), (True, True, True))

    def test_logged_out_and_blocked_reads_are_reported_with_fixes(self) -> None:
        os.environ["FAKE_CLAUDE_LOGGED_IN"] = "0"
        settings = {"permissions": {"blockReadsOutsideWorkingDirectories": True}}
        (self.config_dir / "settings.json").write_text(json.dumps(settings))
        report, _ = self.doctor()
        failed = {c["name"]: c["fix"] for c in report["checks"] if c["ok"] is False and c["required"]}
        settings["permissions"]["additionalDirectories"] = [str(SCRIPTS.parent)]
        (self.config_dir / "settings.json").write_text(json.dumps(settings))
        covered = self.by_name(self.doctor("--offline")[0])["read scope"]
        self.assertEqual(
            (
                report["ready"],
                sorted(failed),
                "claude auth login" in failed["auth"],
                "additionalDirectories" in failed["read scope"],
                covered,
            ),
            (False, ["auth", "lane probe", "read scope"], True, True, True),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
