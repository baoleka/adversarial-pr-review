"""The PreToolUse guard: a default-deny decision matrix for lanes, the author-session rules, the
hook entry point's fail-closed behavior and its heartbeat."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

from support import SCRIPTS

# isort: split


sys.path.insert(0, str(SCRIPTS / "hooks"))
import guard

GUARD = [sys.executable, "-E", "-s", str(SCRIPTS / "hooks" / "guard.py")]
REPLAY = SCRIPTS / "replay.py"


class GuardMatrixTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="adrev-guard-"))
        self.repo = self.tmp / "repo"
        self.run = self.repo / ".reviews" / "run1"
        self.head = self.run / "wt" / "head"
        self.base = self.run / "wt" / "base"
        self.scratch = self.run / "agents" / "r1-breaker-u1"
        self.other_scratch = self.run / "agents" / "r1-external-u1"
        for path in (self.head / "src", self.base / "src", self.scratch, self.other_scratch):
            path.mkdir(parents=True)
        (self.head / "src" / "m.py").write_text("x = 1\n")
        (self.repo / "secret.env").write_text("TOKEN=1\n")
        os.symlink(self.repo / "secret.env", self.scratch / "link.env")
        os.symlink(self.repo, self.scratch / "repo-link")
        self.policy = {
            "lane": "r1-breaker-u1",
            "run": str(self.run),
            "scratch": str(self.scratch),
            "read_roots": [str(self.head), str(self.base), str(self.scratch)],
            "replay": str(REPLAY),
            "heartbeat": str(self.tmp / "guard.log"),
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def replay(self, *extra: str, run: Path | None = None, lane: str = "r1-breaker-u1", file: str | None = None) -> str:
        target = file or str(self.scratch / "test_x.py")
        return " ".join(
            ["python3", "-E", "-s", str(REPLAY), "lane", "--run", str(run or self.run), "--lane", lane, *extra, target]
        )

    def lane_cases(self) -> list[tuple[str, str, dict[str, Any], bool]]:
        s, h, b, r = self.scratch, self.head, self.base, self.repo
        return [
            # Reads
            ("read head file", "Read", {"file_path": str(h / "src" / "m.py")}, True),
            ("read base dir", "Read", {"file_path": str(b / "src")}, True),
            ("read scratch", "Read", {"file_path": str(s / "notes.txt")}, True),
            ("read relative in scratch", "Read", {"file_path": "notes.txt"}, True),
            ("read relative into head", "Read", {"file_path": "../../wt/head/src/m.py"}, True),
            ("read run state", "Read", {"file_path": str(self.run / "state.json")}, False),
            ("read relative run state", "Read", {"file_path": "../../state.json"}, False),
            ("read author tree", "Read", {"file_path": str(r / "secret.env")}, False),
            ("read other lane scratch", "Read", {"file_path": str(self.other_scratch / "out.json")}, False),
            ("read home", "Read", {"file_path": str(Path.home() / ".bashrc")}, False),
            ("read etc", "Read", {"file_path": "/etc/passwd"}, False),
            ("read dotdot escape", "Read", {"file_path": str(h / ".." / ".." / ".." / ".." / "secret.env")}, False),
            ("read symlink to secret", "Read", {"file_path": str(s / "link.env")}, False),
            ("read through dir symlink", "Read", {"file_path": str(s / "repo-link" / "secret.env")}, False),
            ("grep head", "Grep", {"pattern": "x", "path": str(h)}, True),
            ("grep default cwd", "Grep", {"pattern": "x"}, True),
            ("grep repo", "Grep", {"pattern": "TOKEN", "path": str(r)}, False),
            ("glob head", "Glob", {"pattern": "**/*.py", "path": str(h)}, True),
            ("glob absolute outside", "Glob", {"pattern": "/etc/**"}, False),
            ("glob absolute head", "Glob", {"pattern": f"{h}/**/*.py"}, True),
            ("LS outside", "LS", {"path": "/"}, False),
            ("notebook read outside", "NotebookRead", {"notebook_path": "/tmp/x.ipynb"}, False),
            # Writes
            ("write scratch", "Write", {"file_path": str(s / "out.json"), "content": "{}"}, True),
            ("write scratch relative", "Write", {"file_path": "test_x.py", "content": ""}, True),
            ("write scratch subdir", "Write", {"file_path": str(s / "sub" / "a.py"), "content": ""}, True),
            ("write head", "Write", {"file_path": str(h / "src" / "m.py"), "content": ""}, False),
            ("write run state", "Write", {"file_path": str(self.run / "state.json"), "content": ""}, False),
            (
                "write other lane out",
                "Write",
                {"file_path": str(self.other_scratch / "out.json"), "content": ""},
                False,
            ),
            ("write author tree", "Write", {"file_path": str(r / "x.py"), "content": ""}, False),
            (
                "write via dotdot",
                "Write",
                {"file_path": str(s / ".." / "r1-external-u1" / "out.json"), "content": ""},
                False,
            ),
            ("write via symlinked dir", "Write", {"file_path": str(s / "repo-link" / "x.py"), "content": ""}, False),
            ("write through file symlink", "Write", {"file_path": str(s / "link.env"), "content": ""}, False),
            ("write without path", "Write", {"content": "x"}, False),
            ("write tmp", "Write", {"file_path": "/tmp/x", "content": ""}, False),
            ("edit scratch", "Edit", {"file_path": str(s / "out.json"), "old_string": "a", "new_string": "b"}, False),
            ("multiedit", "MultiEdit", {"file_path": str(s / "out.json"), "edits": []}, False),
            ("notebook edit", "NotebookEdit", {"notebook_path": str(s / "n.ipynb"), "new_source": ""}, False),
            # Bash: the replay wrapper only
            ("replay both", "Bash", {"command": self.replay("--side", "both")}, True),
            ("replay default side", "Bash", {"command": self.replay()}, True),
            ("replay head differential", "Bash", {"command": self.replay("--side", "head", "--differential")}, True),
            ("replay quoted path", "Bash", {"command": self.replay().replace(str(REPLAY), f"'{REPLAY}'")}, True),
            ("replay bad side", "Bash", {"command": self.replay("--side", "all")}, False),
            ("replay unknown flag", "Bash", {"command": self.replay("--network")}, False),
            ("replay other lane (adverse #5)", "Bash", {"command": self.replay(lane="r1-external-u1")}, False),
            ("replay other run", "Bash", {"command": self.replay(run=self.tmp / "other-run")}, False),
            ("replay file outside scratch", "Bash", {"command": self.replay(file=str(h / "src" / "m.py"))}, False),
            (
                "replay file in other scratch",
                "Bash",
                {"command": self.replay(file=str(self.other_scratch / "t.py"))},
                False,
            ),
            ("replay two files", "Bash", {"command": self.replay() + f" {s}/b.py"}, False),
            ("replay without -E -s", "Bash", {"command": self.replay().replace("-E -s ", "")}, False),
            (
                "replay copy elsewhere",
                "Bash",
                {"command": self.replay().replace(str(REPLAY), str(s / "replay.py"))},
                False,
            ),
            ("replay redirect", "Bash", {"command": self.replay() + f" > {r}/x"}, False),
            ("replay append", "Bash", {"command": self.replay() + f" >> {r}/x"}, False),
            ("replay and chain", "Bash", {"command": self.replay() + " && touch x"}, False),
            ("replay semicolon", "Bash", {"command": self.replay() + "; rm -rf /"}, False),
            ("replay pipe tee", "Bash", {"command": self.replay() + f" | tee {r}/x"}, False),
            ("replay backticks", "Bash", {"command": self.replay(file="`id`")}, False),
            ("replay subshell", "Bash", {"command": self.replay(file="$(id)")}, False),
            ("replay var", "Bash", {"command": self.replay(file="${HOME}/x.py")}, False),
            ("replay newline", "Bash", {"command": self.replay() + "\ntouch x"}, False),
            ("replay background", "Bash", {"command": self.replay() + " &"}, False),
            ("env prefix", "Bash", {"command": "FOO=1 " + self.replay()}, False),
            ("cd prefix", "Bash", {"command": f"cd {s} && " + self.replay()}, False),
            ("unbalanced quote", "Bash", {"command": self.replay() + " '"}, False),
            ("cat secret", "Bash", {"command": f"cat {r}/secret.env"}, False),
            ("ls", "Bash", {"command": "ls"}, False),
            ("touch author", "Bash", {"command": f"touch {r}/x"}, False),
            ("sed -i", "Bash", {"command": f"sed -i s/x/y/ {h}/src/m.py"}, False),
            ("cp", "Bash", {"command": f"cp {s}/a {r}/a"}, False),
            ("mv", "Bash", {"command": f"mv {s}/a {r}/a"}, False),
            ("pip install", "Bash", {"command": "pip install requests"}, False),
            ("npm install", "Bash", {"command": "npm install left-pad"}, False),
            ("git commit", "Bash", {"command": "git commit -am x"}, False),
            ("git push", "Bash", {"command": "git push origin HEAD"}, False),
            ("git status", "Bash", {"command": "git status"}, False),
            ("curl", "Bash", {"command": "curl https://example.com"}, False),
            ("wget", "Bash", {"command": "wget https://example.com"}, False),
            ("nc", "Bash", {"command": "nc example.com 80"}, False),
            ("python -c", "Bash", {"command": "python3 -c 'print(1)'"}, False),
            ("python script", "Bash", {"command": f"python3 {s}/test_x.py"}, False),
            ("empty command", "Bash", {"command": ""}, False),
            # Everything else
            ("web fetch", "WebFetch", {"url": "https://example.com"}, False),
            ("web search", "WebSearch", {"query": "x"}, False),
            ("agent", "Agent", {"prompt": "x"}, False),
            ("task", "Task", {"prompt": "x"}, False),
            ("mcp tool", "mcp__github__create_issue", {"title": "x"}, False),
            ("skill", "Skill", {"skill": "x"}, False),
            ("unknown", "Frobnicate", {}, False),
            ("malformed input", "Read", "not a dict", False),  # type: ignore[list-item]
        ]

    def test_lane_policy_matrix(self) -> None:
        wrong = []
        cases = self.lane_cases()
        for name, tool, tool_input, expected in cases:
            event = {"tool_name": tool, "tool_input": tool_input, "cwd": str(self.scratch)}
            allowed, reason = guard.decide_lane(self.policy, event)
            if allowed != expected:
                wrong.append(f"{name}: {'allowed' if allowed else 'denied'} ({reason})")
        self.assertEqual((len(cases) >= 60, wrong), (True, []))

    def test_author_session_rules(self) -> None:
        state = str(self.run / "state.json")
        scripts = str(SCRIPTS)
        cases = [
            ("edit code", "Edit", {"file_path": str(self.repo / "src" / "m.py")}, True),
            ("write code", "Write", {"file_path": str(self.repo / "x.py")}, True),
            ("write state", "Write", {"file_path": state}, False),
            ("edit findings", "Edit", {"file_path": str(self.run / "findings.json")}, False),
            ("write worktree", "Write", {"file_path": str(self.head / "src" / "m.py")}, False),
            ("bash redirect into run", "Bash", {"command": f"echo x > {state}"}, False),
            ("bash sed state", "Bash", {"command": f"sed -i s/a/b/ {state}"}, False),
            ("bash rm run", "Bash", {"command": f"rm -rf {self.run}"}, False),
            ("bash cat state", "Bash", {"command": f"cat {state}"}, False),
            ("driver", "Bash", {"command": f"python3 -E -s {scripts}/review.py next --run {self.run}"}, True),
            ("tests", "Bash", {"command": "python -m pytest -q"}, True),
        ]
        wrong = []
        for name, tool, tool_input, expected in cases:
            allowed, reason = guard.decide_author(
                {"tool_name": tool, "tool_input": tool_input, "cwd": str(self.repo)}, scripts
            )
            if allowed != expected:
                wrong.append(f"{name}: {'allowed' if allowed else 'denied'} ({reason})")
        self.assertEqual(wrong, [])

    def hook(self, event: Any, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
        text = event if isinstance(event, str) else json.dumps(event)
        full_env = {k: v for k, v in os.environ.items() if not k.startswith("ADREV_")}
        return subprocess.run(GUARD, input=text, capture_output=True, text=True, env={**full_env, **env}, check=False)

    def test_entry_point_fails_closed_and_logs(self) -> None:
        policy_path = self.tmp / "policy.json"
        policy_path.write_text(json.dumps(self.policy))
        lane_env = {"ADREV_LANE_ID": "r1-breaker-u1", "ADREV_LANE_POLICY": str(policy_path)}
        allow = {
            "tool_name": "Read",
            "tool_input": {"file_path": str(self.head / "src" / "m.py")},
            "cwd": str(self.scratch),
        }
        deny = {"tool_name": "Bash", "tool_input": {"command": "id"}, "cwd": str(self.scratch)}
        results = [
            self.hook(allow, lane_env).returncode,
            self.hook(deny, lane_env),
            self.hook(allow, {"ADREV_LANE_ID": "x"}).returncode,
            self.hook({**allow, "agent_type": "review-breaker"}, {}).returncode,
            self.hook("{not json", lane_env).returncode,
            self.hook(allow, {**lane_env, "ADREV_LANE_POLICY": str(self.tmp / "missing.json")}).returncode,
        ]
        decision = json.loads(results[1].stdout)["hookSpecificOutput"]["permissionDecision"]
        beats = [json.loads(line) for line in Path(self.policy["heartbeat"]).read_text().splitlines()]
        self.assertEqual(
            (results[0], results[1].returncode, decision, results[2:], [(b["tool"], b["allowed"]) for b in beats]),
            (0, 2, "deny", [2, 2, 2, 2], [("Read", True), ("Bash", False)]),
        )


if __name__ == "__main__":
    unittest.main()
