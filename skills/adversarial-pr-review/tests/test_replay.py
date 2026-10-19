"""Replay in Docker: outcome classification, isolation (network, writes, environment, timeout,
output cap), differential stability, and the `lane` entry lanes call through the guard."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from dataclasses import asdict
from pathlib import Path

from support import SCRIPTS, FixtureRepo

# isort: split

import replay

from common import write_json
from config import DEFAULTS

IMAGE = DEFAULTS["replay"]["image"]
VENV = Path.home() / ".cache" / "adversarial-review" / "venvs" / "standin-py3.12"


def replay_env() -> tuple[replay.PythonEnv | None, str | None]:
    if replay.docker_available(IMAGE):
        return None, "docker or the replay image is unavailable"
    if not (VENV / "bin" / "python").exists():
        return None, "no standalone venv to mount"
    py = replay.describe_python(f"{VENV / 'bin'}{os.pathsep}/usr/bin")
    return (py, None) if not py.problem else (None, py.problem)


PY, SKIP = replay_env()

MODULE = "def double(x):\n    return x * 2\n"


@unittest.skipIf(PY is None, f"replay unavailable: {SKIP}")
class ReplayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="adrev-replay-"))
        self.repo = FixtureRepo.create(
            self.tmp, {"src/pkg/__init__.py": "", "src/pkg/m.py": MODULE, "tests/__init__.py": ""}
        )
        self.base = self.repo.git("rev-parse", "master").strip()
        self.repo.write("src/pkg/m.py", MODULE.replace("x * 2", "x * 2 + 1"))
        self.head = self.repo.commit("change")
        assert PY is not None
        self.setup = replay.ReplaySetup(image=IMAGE, python=PY, timeout_s=30, output_cap=65536, python_paths=["src"])

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def side(self, name: str, content: str, commit: str | None = None, **over: object) -> replay.SideOutcome:
        setup = replay.ReplaySetup(**{**asdict(self.setup), "python": PY, **over})  # type: ignore[arg-type]
        work = self.tmp / "work" / f"{name}-{commit or 'head'}"
        return replay.run_side(setup, self.repo.root, commit or self.head, name, content, None, work)

    def test_outcome_classification(self) -> None:
        test = "from pkg.m import double\n\n\ndef test_double():\n    assert double(2) == 4\n"
        results = [
            self.side("test_double.py", test, self.head),
            self.side("test_double.py", test, self.base),
            self.side("check.py", "from pkg.m import double\nassert double(2) == 4, double(2)\n", self.head),
            self.side("broken.py", "def (:\n", self.head),
            self.side("test_broken.py", "import nonexistent_module\n", self.head),
            self.side("slow.py", "import time\ntime.sleep(60)\n", self.head, timeout_s=3),
        ]
        self.assertEqual(
            [(r.outcome, r.signature) for r in results],
            [
                ("failed", "AssertionError: assert 5 == 4"),
                ("pass", None),
                ("failed", "AssertionError: 5"),
                ("error", "SyntaxError: invalid syntax"),
                ("error", "ModuleNotFoundError: No module named 'nonexistent_module'"),
                ("timeout", None),
            ],
        )

    def test_isolation(self) -> None:
        os.environ["CLAUDE_CODE_OAUTH_TOKEN"] = "secret-token-for-test"
        try:
            probe = (
                "import os, socket\n"
                "try:\n    socket.create_connection(('1.1.1.1', 53), timeout=2)\n    print('NET-OPEN')\n"
                "except OSError:\n    print('net-blocked')\n"
                "for target in ('/work/x', '/etc/x', os.path.expanduser('~') + '/x'):\n"
                "    try:\n        open(target, 'w').write('x')\n        print('WROTE', target)\n"
                "    except OSError:\n        print('denied', target)\n"
                "print('ENV', sorted(k for k in os.environ if 'TOKEN' in k or 'KEY' in k or 'CLAUDE' in k))\n"
                "print('UID', os.getuid() != 0)\n"
            )
            out = self.side("isolation.py", probe).output_tail
        finally:
            del os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        flood = self.side("flood.py", "print('x' * 200000)\n", output_cap=4096)
        self.assertEqual(
            (
                "net-blocked" in out and "NET-OPEN" not in out,
                out.count("denied /work/x") + out.count("denied /etc/x"),
                "WROTE /tmp/x" in out,
                "ENV []" in out,
                "UID True" in out,
                (flood.truncated, len(flood.output_tail) <= 4096),
            ),
            (True, 2, True, True, True, (True, True)),
        )

    def test_differential_repro_is_stable(self) -> None:
        script = "from pkg.m import double\nprint(double(21))\n"
        runs = [self.side("diff.py", script, commit) for commit in (self.head, self.head, self.base, self.base)]
        hashes = [r.output_sha256 for r in runs]
        self.assertEqual((hashes[0] == hashes[1], hashes[2] == hashes[3], hashes[0] == hashes[2]), (True, True, False))

    def test_lane_entry(self) -> None:
        run_dir = self.repo.root / ".reviews" / "run1"
        scratch = run_dir / "agents" / "r1-breaker-u1"
        scratch.mkdir(parents=True)
        assert PY is not None
        write_json(
            run_dir / "state.json",
            {
                "repo": str(self.repo.root),
                "round": 1,
                "rounds": {"1": {"snapshot": {"commit": self.head}}},
                "merge_base": self.base,
                "replay_python": asdict(PY),
            },
        )
        cfg = json.loads(json.dumps(DEFAULTS))
        cfg["repo"]["python_paths"] = ["src"]
        write_json(run_dir / "config.json", cfg)
        (scratch / "test_double.py").write_text(
            "from pkg.m import double\n\n\ndef test_double():\n    assert double(2) == 4\n"
        )
        cmd = [
            sys.executable,
            "-E",
            "-s",
            str(SCRIPTS / "replay.py"),
            "lane",
            "--run",
            str(run_dir),
            "--lane",
            "r1-breaker-u1",
        ]
        ok = subprocess.run(
            [*cmd, "--side", "both", str(scratch / "test_double.py")], capture_output=True, text=True, check=False
        )
        outside = subprocess.run(
            [*cmd, str(self.repo.root / "src" / "pkg" / "m.py")], capture_output=True, text=True, check=False
        )
        result = json.loads(ok.stdout)
        records = list((run_dir / "replays" / "lanes" / "r1-breaker-u1").glob("*/record.json"))
        self.assertEqual(
            (
                ok.returncode,
                result["head"]["outcome"],
                result["base"]["outcome"],
                "output_ref" in result["head"],
                len(records),
                records[0].is_relative_to(scratch) if records else None,
                outside.returncode,
                "scratch dir" in outside.stdout,
            ),
            (0, "failed", "pass", False, 1, False, 2, True),
        )


if __name__ == "__main__":
    unittest.main()
