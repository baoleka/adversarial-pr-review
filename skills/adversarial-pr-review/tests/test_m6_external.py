"""M6: the external lane, against a stub codex only (EXTERNAL_LANES=none: no real call is made)."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from support import FixtureRepo

# isort: split

import export
import run_external
from test_forbidden import FORBIDDEN
from test_m3_lanes import CONFIG, DriverCase, TmpCase

from common import read_json
from state import Run

FAKE_CODEX = Path(__file__).resolve().parent / "fakes" / "fake_codex.py"
DENY = [".env*", "**/.env*", "**/secrets/**", "**/*.pem", "**/*.key"]


def codex_bin(tmp: Path) -> Path:
    path = tmp / "bin" / "codex"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{FAKE_CODEX}" "$@"\n')
    path.chmod(0o755)
    return path


class ExportTest(TmpCase):
    def test_denylisted_files_and_links_are_left_out(self) -> None:
        files = {
            "src/app.py": "x = 1\n",
            ".env": "TOKEN=1\n",
            "config/.env.local": "TOKEN=2\n",
            "deploy/secrets/db.txt": "pw\n",
            "certs/server.pem": "-----\n",
            "certs/id.key": "k\n",
            "README.md": "hi\n",
        }
        repo = FixtureRepo.create(self.tmp, files)
        os.symlink("/etc/passwd", repo.root / "link")
        head = repo.commit("add link")
        skipped = export.export(repo.root, head, self.tmp / "export", ["**"], DENY)
        present = sorted(
            str(p.relative_to(self.tmp / "export")) for p in (self.tmp / "export").rglob("*") if p.is_file()
        )
        self.assertEqual(
            (present, skipped),
            (
                ["README.md", "src/app.py"],
                [".env", "certs/id.key", "certs/server.pem", "config/.env.local", "deploy/secrets/db.txt", "link"],
            ),
        )


class CodexRunnerTest(TmpCase):
    def test_argv_and_usage(self) -> None:
        argv = run_external.codex_argv("codex", Path("/x/export"), Path("/s/wire.json"), Path("/x/out.json"), "gpt-x")
        jsonl = "\n".join(
            [
                json.dumps(
                    {
                        "type": "turn.completed",
                        "usage": {
                            "input_tokens": 10,
                            "cached_input_tokens": 4,
                            "output_tokens": 3,
                            "reasoning_output_tokens": 2,
                        },
                    }
                ),
                "not json",
                json.dumps({"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": -1}}),
                json.dumps({"type": "item.completed"}),
            ]
        )
        joined = " ".join(argv)
        self.assertEqual(
            (
                argv,
                [p for p in FORBIDDEN + ["--full-auto", "workspace-write"] if re.search(p, joined)],
                run_external.parse_usage(jsonl),
            ),
            (
                ["codex", "exec", "--sandbox", "read-only", "-C", "/x/export", "--skip-git-repo-check",
                 "--ignore-user-config", "--ephemeral", "--json", "--output-schema", "/s/wire.json", "-o", "/x/out.json",
                 "-m", "gpt-x", "-"],
                [],
                {"input_tokens": 15, "cached_input_tokens": 4, "output_tokens": 3, "reasoning_output_tokens": 2},
            ),
        )  # fmt: skip

    def runner(self, scenario: str) -> dict[str, object]:
        control = self.tmp / f"control-{scenario}"
        (control / "export").mkdir(parents=True)
        (control / "brief.md").write_text("Review this.\n")
        spec = {
            "codex_bin": str(codex_bin(self.tmp)),
            "model": None,
            "export_dir": str(control / "export"),
            "schema": str(control / "schema.json"),
            "brief": str(control / "brief.md"),
            "out": str(control / "out.json"),
            "timeout_s": 60,
        }
        (control / "external.json").write_text(json.dumps(spec))
        os.environ["FAKE_CODEX_SCENARIO"] = scenario
        run_external.main(str(control))
        status: dict[str, object] = json.loads((control / "status.json").read_text())
        status["out"] = (control / "out.json").exists()
        return status

    def test_read_confinement_canary(self) -> None:
        leaky, confined, failing = self.runner("leaky"), self.runner("confined"), self.runner("fail")
        self.assertEqual(
            [(s["status"], s["out"]) for s in (leaky, confined, failing)],
            [("refused", False), ("ok", True), ("refused", False)],
        )


class ExternalLaneDriverTest(DriverCase):
    def start_with_codex(self, scenario: str, installed: bool = True) -> Run:
        config = json.loads(json.dumps(CONFIG))
        config["external"] = {
            "provider": "codex",
            "codex_bin": str(codex_bin(self.tmp)) if installed else "no-such-codex",
        }
        os.environ["FAKE_CODEX_SCENARIO"] = scenario
        original = CONFIG.copy()
        CONFIG.clear()
        CONFIG.update(config)
        try:
            run_dir, _action = self.start({"*": "ok"})
        finally:
            CONFIG.clear()
            CONFIG.update(original)
        return Run.load(run_dir)

    def summary(self, run: Run) -> tuple[list[tuple[str, str]], bool, str | None]:
        verdict = read_json(run.dir / "verdict.json")
        lanes = sorted((k, v["status"]) for k, v in run.state["rounds"]["1"]["lanes"].items())
        return lanes, verdict["single_family"], run.state.get("single_family_reason")

    def test_fallbacks_and_single_family(self) -> None:
        leaky = self.summary(self.start_with_codex("leaky"))
        self.tearDown()
        self.setUp()
        confined = self.summary(self.start_with_codex("confined"))
        self.tearDown()
        self.setUp()
        missing = self.summary(self.start_with_codex("confined", installed=False))
        self.assertEqual(
            (leaky[0], leaky[1], "outside its export dir" in str(leaky[2]), confined[:2], missing[:2], missing[2]),
            (
                [("r1-breaker-u1", "ok"), ("r1-external-u1", "failed"), ("r1-external-u1-claude", "ok")],
                True,
                True,
                ([("r1-breaker-u1", "ok"), ("r1-external-u1", "ok")], False),
                ([("r1-breaker-u1", "ok"), ("r1-external-u1", "ok")], True),
                "the codex CLI is not installed",
            ),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
