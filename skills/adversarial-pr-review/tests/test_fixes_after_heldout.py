"""Fixes made after the first held-out run: unverified minor fixes don't force rounds, the fix
check and lanes guard base behavior and documented contracts, and runs are cleaned up."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from support import FixtureRepo

# isort: split

import findings as fmod
import phases_loop
import phases_verify
from test_m3_lanes import BASE_SRC, CONFIG, HEAD_SRC, REVIEW, TmpCase

from common import PROMPTS_DIR


def finding(severity: str, title: str, line: int) -> dict[str, Any]:
    f = fmod.make(
        origin="lane", category="correctness", severity=severity, title=title, file="pkg/ranges.py",
        start_line=line, quote=f"line {line}", symbol="clamp", conditions="c", expected="e", actual="a",
    )  # fmt: skip
    f["bucket"] = "non-blocking" if severity in ("minor", "nit") else "blocking"
    return f


class PendingFixesTest(TmpCase):
    def test_only_major_fixes_must_verify(self) -> None:
        major, minor = finding("major", "Upper bound excluded", 4), finding("minor", "Unclear message", 9)
        fixes = {major["id"]: {"status": "not-fixed"}, minor["id"]: {"status": "not-fixed"}}
        answers = {
            f["id"]: {
                "action": "fix",
                "note": "done",
                "diverges_from_suggestion": False,
                "reason": None,
                "evidence": [],
            }
            for f in (major, minor)
        }
        carried = {f["title"]: f for f in phases_loop.carry_over([major, minor], answers, fixes, {}, 2)}
        self.assertEqual(
            (
                phases_verify.pending_fixes(fixes, [major, minor]),
                carried["Unclear message"]["status"],
                "not verified" in carried["Unclear message"]["disposition"]["note"],
                carried["Upper bound excluded"]["status"],
            ),
            ([major["id"]], "deferred", True, "not-fixed"),
        )


class PromptRulesTest(TmpCase):
    def test_rules_reach_the_briefs(self) -> None:
        fixcheck = (PROMPTS_DIR / "fixcheck.md").read_text()
        lane = (PROMPTS_DIR / "lane.md").read_text()
        self.assertEqual(
            (
                "--differential" in fixcheck and "REGRESSED" in fixcheck and "fallback" in fixcheck,
                "does not fix a finding about a" in fixcheck,
                "The intent can't make such a break safe" in lane,
                "restores the base behavior" in lane,
            ),
            (True, True, True, True),
        )


class RetentionTest(TmpCase):
    def test_old_runs_caches_and_worktrees_are_removed(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["limits"] = {"retention": {"keep_runs": 2, "keep_worktrees": False}}
        repo = FixtureRepo.create(
            self.tmp, {"pkg/ranges.py": BASE_SRC, "pkg/__init__.py": "", "review.config.json": json.dumps(config)}
        )
        repo.write("pkg/ranges.py", HEAD_SRC)
        repo.commit("change")
        self.plan({"*": "ok"})
        run_dirs = []
        for _ in range(3):
            out = json.loads(
                subprocess.run(
                    [*REVIEW, "start", "--no-fix", "x"], cwd=repo.root, capture_output=True, text=True, check=True
                ).stdout
            )
            action = out["action"]
            while action["kind"] in ("exec", "wait"):
                action = json.loads(
                    subprocess.run(action["argv"], cwd=repo.root, capture_output=True, text=True, check=True).stdout
                )["action"]
            run_dirs.append(Path(out["run"]))
        worktrees = subprocess.run(
            ["git", "-C", str(repo.root), "worktree", "list"], capture_output=True, text=True, check=True
        ).stdout
        self.assertEqual(
            (
                [d.exists() for d in run_dirs],
                (run_dirs[-1] / "cache").exists(),
                (run_dirs[-1] / "report.md").is_file(),
                worktrees.count("\n"),
            ),
            ([False, True, True], False, True, 1),
        )
