"""M5: dispositions, fix verification, adjudication, late-finding and suppression rules, and the
fix loop end to end through the driver with fake lanes (the test plays the author)."""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from support import SCRIPTS, FixtureRepo

# isort: split

import diffs
import findings as fmod
import phases_loop
from test_m3_lanes import BASE_SRC, CONFIG, HEAD_SRC, TmpCase, wire_finding

import dispositions
from common import ReviewError, read_json
from state import Run

REVIEW = [sys.executable, "-E", "-s", str(SCRIPTS / "review.py")]
EXTRA = "\n\ndef triple(x):\n    return x * 3\n"
FIXED_SRC = BASE_SRC + EXTRA


def finding(origin: str = "lane", bucket: str = "blocking", severity: str = "major", **over: Any) -> dict[str, Any]:
    f = fmod.make(
        origin=origin, category="correctness", severity=severity, title=over.pop("title", "Upper bound excluded"),
        file="pkg/ranges.py", start_line=over.pop("line", 4), quote=over.pop("quote", "if value >= high:"),
        symbol="clamp", conditions="clamp(10, 0, 10)", expected="10", actual="9",
    )  # fmt: skip
    f["bucket"] = bucket
    f.update(over)
    return f


class DispositionRulesTest(TmpCase):
    def test_record_validation(self) -> None:
        _repo, run = self.make_run()
        roots = {"head": run.wt_head, "base": run.wt_base}
        lane = finding()
        gate = finding("gate", title="New mypy error", quote="x", line=1)
        minor = finding(bucket="non-blocking", severity="minor", title="Unclear name", quote="y", line=2)
        found = [lane, gate, minor]
        pending: dict[str, Any] = {}

        def attempt(
            fid: str,
            action: str,
            note: str = "n",
            cites: tuple[str, ...] = (),
            diverges: bool = False,
            reason: str | None = None,
        ) -> str:
            try:
                dispositions.record(found, pending, fid, action, note, diverges, reason, list(cites), roots)
                return "ok"
            except ReviewError as e:
                return str(e).split(":")[0][:40]

        results = [
            attempt(gate["id"], "dispute", cites=("head:pkg/ranges.py:4",)),
            attempt(lane["id"], "defer"),
            attempt(lane["id"], "dispute"),
            attempt(lane["id"], "dispute", cites=("head:pkg/../../../etc/passwd:1",)),
            attempt(lane["id"], "fix", diverges=True),
            attempt(lane["id"], "fix", note=" "),
            attempt(lane["id"], "refuted"),
            attempt("F-000000000000", "fix"),
            attempt(minor["id"], "defer"),
            attempt(lane["id"], "dispute", cites=("head:pkg/ranges.py:4",)),
            attempt(gate["id"], "fix"),
        ]
        self.assertEqual(
            (
                [r == "ok" for r in results],
                pending[lane["id"]]["evidence"][0]["quote"],
                dispositions.missing(found, pending),
            ),
            (
                [False, False, False, False, False, False, False, False, True, True, True],
                "if value >= high:",
                [],
            ),
        )


class LateAndSuppressionTest(TmpCase):
    def test_rules(self) -> None:
        repo, run = self.make_run()
        delta = {
            "pkg/ranges.py": diffs.FileDiff(
                "pkg/ranges.py", "pkg/ranges.py", "modified", added={10: "    return x * 4"}
            )
        }
        refuted = finding(status="refuted")
        same = finding()
        moved_code = finding(
            line=10, quote="return x * 4", introduced_by=[{"side": "head", "file": "pkg/ranges.py", "line": 10}]
        )
        new_evidence = finding(replay={"head": {}, "base": {}, "classification": "introduced", "reason": ""})
        other_scenario = finding(failure_scenario={"conditions": "clamp(11, 0, 10)", "expected": "10", "actual": "9"})
        late_minor = finding(title="Other issue", quote="return value", line=6, severity="minor")
        late_major_executed = finding(
            title="Executed issue",
            quote="return value",
            line=6,
            replay={"head": {}, "base": {}, "classification": "introduced", "reason": ""},
        )
        rule = phases_loop.late_or_suppressed
        self.assertEqual(
            [
                rule(same, [refuted], delta),
                rule(moved_code, [refuted], delta),
                rule(new_evidence, [refuted], delta),
                rule(other_scenario, [refuted], delta),
                rule(late_minor, [], delta),
                rule(late_major_executed, [], delta),
            ],
            ["suppressed", None, None, "late", "late", None],
        )


class LoopTest(TmpCase):
    def repo(self, config: dict[str, Any] | None = None) -> FixtureRepo:
        files = {"pkg/ranges.py": BASE_SRC, "pkg/__init__.py": "", "review.config.json": json.dumps(config or CONFIG)}
        repo = FixtureRepo.create(self.tmp, files)
        repo.write("pkg/ranges.py", HEAD_SRC + EXTRA)
        repo.commit("change clamp and add triple")
        return repo

    def review(self, repo: FixtureRepo, *args: str) -> dict[str, Any]:
        proc = subprocess.run([*REVIEW, *args], cwd=repo.root, capture_output=True, text=True, check=False)
        if proc.returncode != 0:
            raise AssertionError(f"review.py {args[:2]} failed: {proc.stdout}{proc.stderr}")
        out: dict[str, Any] = json.loads(proc.stdout)
        return out

    def loop(
        self,
        plan: dict[str, str],
        author: Callable[[FixtureRepo, dict[str, Any], int], None],
        config: dict[str, Any] | None = None,
    ) -> tuple[Run, dict[str, Any], list[str]]:
        data = self.tmp / "breaker-findings.json"
        data.write_text(json.dumps({"findings": [wire_finding()], "notes": None}))
        self.plan({"r1-breaker": f"findings:{data}", **plan, "*": "ok"})
        repo = self.repo(config)
        out = self.review(repo, "start", "Fix", "clamp")
        run_dir, action = Path(out["run"]), out["action"]
        kinds = []
        for _ in range(80):
            kinds.append(action["kind"])
            if action["kind"] in ("exec", "wait"):
                action = json.loads(
                    subprocess.run(action["argv"], cwd=repo.root, capture_output=True, text=True, check=True).stdout
                )["action"]
            elif action["kind"] == "author":
                author(repo, action, len([k for k in kinds if k == "author"]))
                action = json.loads(
                    subprocess.run(
                        action["continue"].split(), cwd=repo.root, capture_output=True, text=True, check=True
                    ).stdout
                )["action"]
            else:
                break
        return Run.load(run_dir), action, [k for k in kinds if k not in ("exec", "wait")]

    def fix(self, repo: FixtureRepo, action: dict[str, Any], _n: int) -> None:
        repo.write("pkg/ranges.py", FIXED_SRC)
        for item in action["findings"]:
            self.review(repo, "dispose", item["id"], "--action", "fix", "--note", "Restored the inclusive bound.")

    def dispute(self, repo: FixtureRepo, action: dict[str, Any], _n: int) -> None:
        for item in action["findings"]:
            self.review(
                repo,
                "dispose",
                item["id"],
                "--action",
                "dispute",
                "--note",
                "The bound is exclusive on purpose.",
                "--cite",
                "head:pkg/ranges.py:4",
            )

    def conditions(self, run: Run) -> list[str]:
        return sorted({e["condition"] for e in run.state["escalations"]})

    def test_fix_is_verified_and_approved(self) -> None:
        run, action, kinds = self.loop({"verifier": "verify-confirm"}, self.fix)
        final = read_json(run.dir / "findings.json")["rounds"]["2"]["final"]
        self.assertEqual(
            (
                action["verdict"],
                kinds,
                run.state["round"],
                [(f["status"], f["bucket"]) for f in final if f["origin"] == "lane"],
            ),
            ("APPROVE", ["author", "done"], 2, [("fixed", "non-blocking")]),
        )

    def test_not_fixed_keeps_the_finding_open_until_the_round_cap(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["limits"] = {"max_rounds": 2}
        run, action, kinds = self.loop(
            {"verifier": "verify-confirm", "fixcheck": "fixcheck-notfixed"}, self.fix, config
        )
        final = read_json(run.dir / "findings.json")["rounds"]["2"]["final"]
        self.assertEqual(
            (action["verdict"], kinds, self.conditions(run), [f["status"] for f in final if f["origin"] == "lane"]),
            ("ESCALATE", ["author", "escalate"], ["round-cap"], ["not-fixed"]),
        )

    def test_a_failed_fix_that_still_blocks_ends_the_loop(self) -> None:
        # The same blocking set after an author round is an ESCALATE condition (§6), so a fix
        # ruled REGRESSED (or NOT_FIXED) never converges into APPROVE.
        run, action, kinds = self.loop({"verifier": "verify-confirm", "fixcheck": "fixcheck-regressed"}, self.fix)
        statuses = [
            f["status"] for f in read_json(run.dir / "findings.json")["rounds"]["2"]["final"] if f["origin"] == "lane"
        ]
        self.assertEqual(
            (action["verdict"], kinds, self.conditions(run), statuses),
            ("ESCALATE", ["author", "escalate"], ["blocking-unchanged-2-rounds"], ["regressed"]),
        )

    def test_adjudication_outcomes(self) -> None:
        outcomes = {}
        for scenario in ("adjudicate-uphold", "adjudicate-reject", "adjudicate-unresolved"):
            run, action, _kinds = self.loop({"verifier": "verify-confirm", "adjudicator": scenario}, self.dispute)
            final = read_json(run.dir / "findings.json")["rounds"]["2"]["final"]
            lane = [f for f in final if f["origin"] == "lane"]
            outcomes[scenario] = (action["verdict"], self.conditions(run), [(f["status"], f["bucket"]) for f in lane])
            self.tearDown()
            self.setUp()
        self.assertEqual(
            outcomes,
            {
                "adjudicate-uphold": ("ESCALATE", ["blocking-unchanged-2-rounds"], [("open", "blocking")]),
                "adjudicate-reject": ("APPROVE", [], [("refuted-by-adjudication", "non-blocking")]),
                "adjudicate-unresolved": ("ESCALATE", ["adjudication-unresolved"], [("open", "blocking")]),
            },
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
