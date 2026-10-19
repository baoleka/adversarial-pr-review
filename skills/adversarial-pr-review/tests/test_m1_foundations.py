import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import SKILL_DIR, FixtureRepo, sh

# isort: split

import config as config_mod
import ledger
import snapshot as snap
from common import ReviewError
from state import Run, TamperError

REVIEW = [sys.executable, "-E", "-s", str(SKILL_DIR / "scripts" / "review.py")]
# A copy of the benchmark stand-in repo's config (evals/overlay/review.config.json; an evals test keeps
# the two identical), so the skill's tests run without the evals tree.
STANDIN_CONFIG = SKILL_DIR / "tests" / "fixtures" / "standin.review.config.json"


def review(repo: Path, *args: str) -> dict:
    proc = subprocess.run([*REVIEW, *args], cwd=repo, capture_output=True, text=True, check=False)
    if proc.returncode != 0:
        raise AssertionError(f"review.py {args} exited {proc.returncode}: {proc.stdout}\n{proc.stderr}")
    return json.loads(proc.stdout)


class TmpCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adrev-test-"))

    def tearDown(self):
        for root, dirs, _files in os.walk(self.tmp):
            for name in dirs:
                os.chmod(os.path.join(root, name), 0o755)
        shutil.rmtree(self.tmp, ignore_errors=True)


class SnapshotTest(TmpCase):
    def test_snapshot_leaves_index_head_and_files_untouched(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n", ".gitignore": "ignored.txt\n"})
        repo.write("a.py", "x = 2\n")
        repo.git("add", "a.py")
        repo.write("a.py", "x = 3\n")
        repo.write("new.py", "y = 1\n")
        repo.write("ignored.txt", "secret\n")
        index = snap.git_path(repo.root, "index")
        status = repo.git("status", "--porcelain")  # may refresh the index; read it afterwards
        before = (index.read_bytes(), repo.git("rev-parse", "HEAD"), status, os.stat(repo.root / "a.py").st_mtime_ns)
        shot = snap.take_snapshot(repo.root, self.tmp / "scratch", ".reviews", "run1", 1, 1 << 20)
        after_index = index.read_bytes()
        after = (
            after_index,
            repo.git("rev-parse", "HEAD"),
            repo.git("status", "--porcelain"),
            os.stat(repo.root / "a.py").st_mtime_ns,
        )
        files = set(sh(repo.root, "git", "ls-tree", "-r", "--name-only", shot.tree).split())
        content = sh(repo.root, "git", "show", f"{shot.tree}:a.py")
        self.assertEqual((before, files, content), (after, {".gitignore", "a.py", "new.py"}, "x = 3\n"))

    def test_snapshot_excludes_run_dir_and_large_untracked_files(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n"})
        snap.ensure_excluded(repo.root, ".reviews")
        repo.write(".reviews/run/state.json", "{}")
        repo.write("big.bin", "x" * 5000)
        repo.write("small.txt", "ok")
        shot = snap.take_snapshot(repo.root, self.tmp / "scratch", ".reviews", "run1", 1, 1000)
        files = set(sh(repo.root, "git", "ls-tree", "-r", "--name-only", shot.tree).split())
        self.assertEqual((files, shot.skipped_untracked), ({"a.py", "small.txt"}, ["big.bin"]))

    def test_snapshot_commit_is_deterministic_and_referenced(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n"})
        repo.write("a.py", "x = 2\n")
        first = snap.take_snapshot(repo.root, self.tmp / "s1", ".reviews", "run1", 1, 1 << 20)
        second = snap.take_snapshot(repo.root, self.tmp / "s2", ".reviews", "run2", 1, 1 << 20)
        ref = repo.git("rev-parse", "refs/reviews/run1/r1").strip()
        self.assertEqual((first.commit, ref), (second.commit, first.commit))

    def test_run_dir_is_excluded_via_info_exclude_only(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n", ".gitignore": "*.log\n"})
        snap.ensure_excluded(repo.root, ".reviews")
        snap.ensure_excluded(repo.root, ".reviews")
        exclude = snap.git_path(repo.root, "info/exclude").read_text()
        self.assertEqual(
            (exclude.count("/.reviews/"), (repo.root / ".gitignore").read_text()),
            (1, "*.log\n"),
        )


class BaseRefTest(TmpCase):
    def test_base_ref_order(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n"})
        repo.git("update-ref", "refs/remotes/origin/main", "HEAD")
        found = [
            snap.resolve_base_ref(repo.root, "origin/main", "origin"),
            snap.resolve_base_ref(repo.root, None, "origin"),
            snap.resolve_base_ref(repo.root, "does/not/exist", "origin"),
        ]
        repo.git("symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        found.append(snap.resolve_base_ref(repo.root, None, "origin"))
        repo.git("update-ref", "-d", "refs/remotes/origin/main")
        found.append(snap.resolve_base_ref(repo.root, None, "origin"))
        repo.git("update-ref", "-d", "refs/remotes/origin/master")
        found.append(snap.resolve_base_ref(repo.root, None, "origin"))
        self.assertEqual(found, ["origin/main", "origin/HEAD", "origin/HEAD", "origin/main", "origin/master", None])

    def test_criss_cross_history_has_two_merge_bases(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n"})
        repo.git("checkout", "-q", "master")
        repo.write("m1.py", "1")
        m1 = repo.commit("m1")
        repo.git("checkout", "-q", "feature")
        repo.write("f1.py", "1")
        f1 = repo.commit("f1")
        repo.git("merge", "-q", "--no-edit", m1)
        repo.git("checkout", "-q", "master")
        repo.git("merge", "-q", "--no-edit", f1)
        repo.git("update-ref", "refs/remotes/origin/master", "HEAD")
        repo.git("checkout", "-q", "feature")
        repo.write("f2.py", "2")
        repo.commit("f2")
        self.assertEqual(len(snap.merge_bases(repo.root, "origin/master", "HEAD")), 2)

    def test_stale_base_warning(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n"})
        self.assertIsNone(snap.stale_base_warning(repo.root, "origin/master", "origin", 72))
        repo.git("checkout", "-q", "master")
        repo.write("b.py", "1")
        repo.commit("local only")
        self.assertIn(
            "differs from origin/master", snap.stale_base_warning(repo.root, "origin/master", "origin", 72) or ""
        )


class WorktreeTest(TmpCase):
    def test_worktree_runs_no_hooks_and_can_be_made_read_only(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n"})
        hook = repo.root / ".git" / "hooks" / "post-checkout"
        hook.write_text('#!/bin/sh\ntouch "$GIT_DIR/../hook-ran"\n')
        hook.chmod(0o755)
        wt = self.tmp / "wt"
        snap.add_worktree(repo.root, wt, repo.git("rev-parse", "HEAD").strip(), self.tmp / "no-hooks")
        snap.set_writable(wt, False)
        with self.assertRaises(PermissionError):
            (wt / "a.py").write_text("changed")
        snap.set_writable(wt, True)
        (wt / "a.py").write_text("changed")
        dirty = snap.worktree_dirty(wt)
        snap.reset_worktree(wt, repo.git("rev-parse", "HEAD").strip())
        self.assertEqual(
            ((repo.root / "hook-ran").exists(), bool(dirty), snap.worktree_dirty(wt)),
            (False, True, ""),
        )
        snap.remove_worktree(repo.root, wt)
        self.assertFalse(wt.exists())


class ConfigTest(TmpCase):
    def test_standin_config_is_valid(self):
        raw, errors = config_mod.parse(STANDIN_CONFIG.read_text(), "stand-in")
        self.assertEqual(errors, [])

    def test_config_comes_from_merge_base_not_the_working_tree(self):
        repo = FixtureRepo.create(
            self.tmp,
            {
                "review.config.json": json.dumps(
                    {
                        "schema_version": "1",
                        "repo": {
                            "base_ref": "origin/master",
                            "remote": "origin",
                            "gates": [],
                            "changelogs": [],
                            "conventions_docs": [],
                            "file_classes": {},
                            "sensitive_paths": ["base-only"],
                            "security_patterns": [],
                        },
                    }
                )
            },
        )
        merge_base = repo.git("rev-parse", "HEAD").strip()
        repo.write(
            "review.config.json",
            json.dumps(
                {
                    "schema_version": "1",
                    "repo": {
                        "base_ref": None,
                        "remote": "origin",
                        "gates": [],
                        "changelogs": [],
                        "conventions_docs": [],
                        "file_classes": {},
                        "sensitive_paths": ["head-only"],
                        "security_patterns": [],
                    },
                }
            ),
        )
        repo.commit("change config")
        loaded = config_mod.load(repo.root, merge_base, head_root=repo.root)
        self.assertEqual((loaded.source, loaded.data["repo"]["sensitive_paths"]), ("merge-base", ["base-only"]))

    def test_missing_merge_base_config_falls_back_to_head(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x"})
        merge_base = repo.git("rev-parse", "HEAD").strip()
        repo.write(".claude/review.config.json", STANDIN_CONFIG.read_text())
        loaded = config_mod.load(repo.root, merge_base, head_root=repo.root)
        self.assertEqual((loaded.source, loaded.path), ("head", ".claude/review.config.json"))

    def test_invalid_config_fails_closed(self):
        repo = FixtureRepo.create(
            self.tmp, {"review.config.json": '{"schema_version": "1", "repo": {"gates": "nope"}}'}
        )
        with self.assertRaises(ReviewError):
            config_mod.load(repo.root, repo.git("rev-parse", "HEAD").strip())


class StateTest(TmpCase):
    def test_tampering_with_a_state_file_is_detected(self):
        run = Run.create(self.tmp, "r1", {"phase": "INIT", "round": 1})
        run.write_state_file("verdict.json", {"verdict": "REQUEST_CHANGES"})
        (run.dir / "verdict.json").write_text('{"verdict": "APPROVE"}\n')
        with self.assertRaises(TamperError):
            Run.load(run.dir)

    def test_unexpected_state_file_is_detected(self):
        run = Run.create(self.tmp, "r1", {"phase": "INIT", "round": 1})
        (run.dir / "findings.json").write_text("{}")
        with self.assertRaises(TamperError):
            Run.load(run.dir)

    def test_jobs_report_exit_codes_and_timeouts(self):
        run = Run.create(self.tmp, "r1", {"phase": "INIT", "round": 1})
        env = dict(os.environ)
        ok = run.spawn_job("t", [sys.executable, "-c", "print('hi')"], self.tmp, env, 30)
        bad = run.spawn_job("t", [sys.executable, "-c", "import sys; sys.exit(3)"], self.tmp, env, 30)
        slow = run.spawn_job("t", [sys.executable, "-c", "import time; time.sleep(30)"], self.tmp, env, 1)
        deadline = time.time() + 20
        while time.time() < deadline and any(run.poll_job(j) == "running" for j in (ok, bad, slow)):
            time.sleep(0.2)
        self.assertEqual(
            [(run.state["jobs"][j]["status"], run.state["jobs"][j]["exit_code"]) for j in (ok, bad, slow)]
            + [run.job_output(ok).strip()],
            [("done", 0), ("done", 3), ("timeout", None), "hi"],
        )


class LedgerTest(unittest.TestCase):
    def test_reopen_counting(self):
        data = ledger.empty("run")
        ledger.record_round(data, 1, [{"id": "F-1", "fingerprint": "f", "status": "open"}])
        ledger.record_round(data, 2, [{"id": "F-1", "fingerprint": "f", "status": "fixed"}])
        ledger.record_round(data, 3, [{"id": "F-1", "fingerprint": "f", "status": "open"}])
        ledger.record_round(data, 4, [{"id": "F-1", "fingerprint": "f", "status": "fixed"}])
        ledger.record_round(data, 5, [{"id": "F-1", "fingerprint": "f", "status": "regressed"}])
        entry = data["fingerprints"]["f"]
        self.assertEqual(
            (entry["reopen_count"], entry["rounds_seen"], ledger.reopened_twice(data)), (2, [1, 2, 3, 4, 5], ["f"])
        )


class DriverTest(TmpCase):
    def make_repo(self):
        repo = FixtureRepo.create(self.tmp, {"pkg/a.py": "def f():\n    return 1\n"})
        repo.write("pkg/a.py", "def f():\n    return 2\n")
        repo.commit("change f")
        return repo

    def test_start_snapshots_resolves_base_and_reaches_a_verdict(self):
        repo = self.make_repo()
        out = review(repo.root, "start", "--no-fix", "change", "f")
        action = out["action"]
        while action["kind"] in ("exec", "wait"):
            action = review(repo.root, *action["argv"][4:])["action"]
        run = Run.load(Path(out["run"]))
        self.assertEqual(
            (
                action["kind"],
                run.state["base_ref"],
                run.state["merge_base"],
                run.state["intent"],
                repo.git("show", f"{run.state['rounds']['1']['snapshot']['commit']}:pkg/a.py"),
            ),
            (
                "done",
                "origin/HEAD",
                repo.git("rev-parse", "origin/master").strip(),
                "change f",
                "def f():\n    return 2\n",
            ),
        )

    def test_intent_is_drafted_from_commits_when_missing(self):
        repo = self.make_repo()
        out = review(repo.root, "start", "--no-fix")
        run = Run.load(Path(out["run"]))
        self.assertEqual((run.state["intent_source"], "change f" in run.state["intent"]), ("drafted", True))

    def test_missing_base_ref_asks_the_user(self):
        repo = self.make_repo()
        repo.git("symbolic-ref", "-d", "refs/remotes/origin/HEAD")
        repo.git("update-ref", "-d", "refs/remotes/origin/master")
        out = review(repo.root, "start", "--no-fix", "x")
        self.assertEqual(out["action"]["kind"], "ask")
        out = review(repo.root, "ingest", out["action"]["id"], "--answer", "master")
        self.assertEqual(Run.load(Path(out["run"])).state["base_ref"], "master")

    def test_tampered_state_escalates(self):
        repo = self.make_repo()
        out = review(repo.root, "start", "--no-fix", "x")
        state = Path(out["run"]) / "state.json"
        state.write_text(state.read_text().replace('"no-fix"', '"loop"'))
        result = review(repo.root, "next")
        self.assertEqual(result["action"]["kind"], "escalate")

    def test_repeated_identical_action_escalates(self):
        repo = self.make_repo()
        out = review(repo.root, "start", "--no-fix", "x")
        run = Run.load(Path(out["run"]))
        run.state["phase"] = "GATES"
        run.state["terminal"] = None
        run.state["actions"] = []
        run.state["waiting_on"] = []
        run.save()
        results = [review(repo.root, "next")["action"]["kind"] for _ in range(3)]
        self.assertEqual(results, ["exec", "exec", "escalate"])


if __name__ == "__main__":
    unittest.main()


class DriverRecoveryTest(TmpCase):
    def test_ambiguous_merge_base_escalates(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n"})
        repo.git("checkout", "-q", "master")
        repo.write("m1.py", "1")
        m1 = repo.commit("m1")
        repo.git("checkout", "-q", "feature")
        repo.write("f1.py", "1")
        f1 = repo.commit("f1")
        repo.git("merge", "-q", "--no-edit", m1)
        repo.git("checkout", "-q", "master")
        repo.git("merge", "-q", "--no-edit", f1)
        repo.git("update-ref", "refs/remotes/origin/master", "HEAD")
        repo.git("checkout", "-q", "feature")
        out = review(repo.root, "start", "--no-fix", "x")
        run = Run.load(Path(out["run"]))
        self.assertEqual((out["action"]["kind"], run.state["escalations"][0]["condition"]), ("escalate", "merge-base"))

    def test_resume_retries_a_crashed_phase_once_then_fails(self):
        repo = FixtureRepo.create(self.tmp, {"a.py": "x = 1\n"})
        repo.write("a.py", "x = 2\n")
        repo.commit("c")
        out = review(repo.root, "start", "--no-fix", "x")
        run = Run.load(Path(out["run"]))
        # Pretend the GATES phase crashed twice in a row.
        run.state.update(
            {
                "phase": "GATES",
                "terminal": None,
                "escalations": [],
                "in_progress": {"phase": "GATES", "round": 1, "attempts": 2},
            }
        )
        run.save()
        action = review(repo.root, "next")["action"]
        final = review(repo.root, *action["argv"][4:])["action"]
        run = Run.load(Path(out["run"]))
        self.assertEqual(
            (final["kind"], [e["detail"] for e in run.state["escalations"]][:1]),
            ("escalate", ["phase GATES failed twice"]),
        )
