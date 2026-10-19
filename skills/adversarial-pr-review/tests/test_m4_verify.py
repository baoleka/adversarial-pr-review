"""M4: replay classification, verification under the evidence bar, synthesis, verdict rules,
report determinism, and verdicts end to end through the driver (fake claude)."""

from __future__ import annotations

import copy
import json
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from support import FixtureRepo

# isort: split

import findings as fmod
import replay
import report
import synthesize
import verdict as verdict_mod
import verify
from replay import SideOutcome
from test_m3_lanes import BASE_SRC, HEAD_SRC, DriverCase, wire_finding
from test_replay import PY, SKIP

from common import SCHEMAS_DIR, read_json
from config import DEFAULTS
from jsonschema_lite import validate
from state import Run

ALLOW = DEFAULTS["replay"]["allow"]


def side(outcome: str, signature: str | None = None, sha: str = "s1") -> SideOutcome:
    return SideOutcome(outcome, 1 if outcome == "failed" else 0, signature, None, "", sha, 0.1)


def lane_finding(severity: str = "major", **over: Any) -> dict[str, Any]:
    line = over.pop("line", 4)
    f = fmod.make(
        origin="lane",
        category="correctness",
        severity=severity,
        title=over.pop("title", "Upper bound is no longer inclusive"),
        file=over.pop("file", "pkg/ranges.py"),
        start_line=line,
        quote=over.pop("quote", "if value >= high:" if line == 4 else f"line {line}"),
        symbol="clamp",
        conditions="clamp(10, 0, 10)",
        expected="10",
        actual="9",
        evidence_level=over.pop("level", "traced"),
        suggested_fix="SECRET-FIX-TEXT",
        observed_output="SECRET-OBSERVED-TEXT",
        citation_chain=[{"side": "head", "file": "pkg/ranges.py", "line": 5, "quote": "SECRET-CHAIN-QUOTE"}],
        lane="r1-breaker-u1",
        model="claude-opus-5-5",
        lens="breaker",
    )
    f.update(over)
    return f


class ReplayClassificationTest(unittest.TestCase):
    def test_classification_table(self) -> None:
        test = {"kind": "test", "expected_failure": "AssertionError"}
        typed = {"kind": "test", "expected_failure": "TypeError: unsupported operand"}
        diff = {"kind": "differential"}
        cases = [
            (test, [side("failed", "AssertionError: assert 9 == 10")], [side("pass")], "introduced"),
            (test, [side("failed", "AssertionError: x")], [side("failed", "AssertionError: x")], "pre-existing"),
            (test, [side("failed", "AssertionError: x")], [side("failed", "AssertionError: y")], "not-demonstrated"),
            (test, [side("pass")], [side("pass")], "not-demonstrated"),
            (test, [side("error"), side("error")], [side("pass")], "not-demonstrated"),
            (test, [side("failed", "TypeError: bad")], [side("pass")], "not-demonstrated"),
            (typed, [side("failed", "TypeError: bad")], [side("pass")], "introduced"),
            (test, [side("failed", None)], [side("pass")], "introduced"),
            (
                test,
                [side("failed", "AssertionError: x")],
                [side("error", "ImportError: cannot import name 'clamp_new' from 'pkg'")],
                "introduced",
            ),
            (
                test,
                [side("failed", "AssertionError: x")],
                [side("error", "ImportError: cannot import name 'old' from 'pkg'")],
                "not-demonstrated",
            ),
            (
                diff,
                [side("pass", sha="a"), side("pass", sha="a")],
                [side("pass", sha="b"), side("pass", sha="b")],
                "introduced",
            ),
            (
                diff,
                [side("pass", sha="a"), side("pass", sha="c")],
                [side("pass", sha="b"), side("pass", sha="b")],
                "not-demonstrated",
            ),
            (
                diff,
                [side("pass", sha="a"), side("pass", sha="a")],
                [side("pass", sha="a"), side("pass", sha="a")],
                "not-demonstrated",
            ),
            (
                diff,
                [side("failed", sha="a"), side("failed", sha="a")],
                [side("failed", sha="a"), side("failed", sha="a")],
                "not-demonstrated",
            ),
        ]
        got = [replay.classify(repro, head, base, "def clamp_new(x):\n")[0] for repro, head, base, _ in cases]
        self.assertEqual(got, [c[3] for c in cases])

    def test_repro_plan(self) -> None:
        self.assertEqual(
            [
                replay.repro_plan({"kind": "test", "filename": "clamp_check.py"}, ALLOW),
                replay.repro_plan({"kind": "command", "filename": "show.py"}, ALLOW),
                replay.repro_plan({"kind": "test", "filename": "../evil.py"}, ALLOW),
                replay.repro_plan({"kind": "command", "filename": "show.py"}, [["python", "-m", "pytest", "{repro}"]]),
            ],
            [
                ("test_clamp_check.py", ["python", "-m", "pytest", "-q", "--tb=short", "-p", "no:randomly", "{repro}"]),
                ("show.py", ["python", "{repro}"]),
                None,
                None,
            ],
        )


@unittest.skipIf(PY is None, f"replay unavailable: {SKIP}")
class ReplayFindingTest(unittest.TestCase):
    def test_findings_replay_at_both_sides(self) -> None:
        tmp = Path(tempfile.mkdtemp(prefix="adrev-m4-"))
        try:
            repo = FixtureRepo.create(
                tmp, {"src/pkg/__init__.py": "", "src/pkg/ranges.py": BASE_SRC, "tests/__init__.py": ""}
            )
            base = repo.git("rev-parse", "master").strip()
            repo.write("src/pkg/ranges.py", HEAD_SRC)
            head = repo.commit("change")
            assert PY is not None
            setup = replay.ReplaySetup(
                image=DEFAULTS["replay"]["image"], python=PY, timeout_s=30, output_cap=65536, python_paths=["src"]
            )
            test = "from pkg.ranges import clamp\n\n\ndef test_upper():\n    assert clamp(10, 0, 10) == 10\n"
            diff = "from pkg.ranges import clamp\nprint(clamp(10, 0, 10))\n"
            results = []
            for kind, name, content in (("test", "test_upper.py", test), ("differential", "show.py", diff)):
                repro = {
                    "kind": kind,
                    "filename": name,
                    "content": content,
                    "command": [],
                    "expected_failure": "AssertionError",
                    "expected_difference": "9 instead of 10",
                }
                finding = {"evidence": {"repro": repro}}
                results.append(
                    replay.replay_finding(
                        setup, repo.root, {"head": head, "base": base}, finding, ALLOW, "", tmp / kind
                    )
                )
            self.assertEqual(
                [(r["classification"], r["head"]["outcome"], r["base"]["outcome"], r["head"]["runs"]) for r in results],
                [("introduced", "fail-as-claimed", "pass", 1), ("introduced", "pass", "pass", 2)],
            )
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class VerificationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="adrev-m4v-"))
        for side_name, text in (("head", HEAD_SRC), ("base", BASE_SRC)):
            (self.tmp / side_name / "pkg").mkdir(parents=True)
            (self.tmp / side_name / "pkg" / "ranges.py").write_text(text)
        self.roots = {"head": self.tmp / "head", "base": self.tmp / "base"}

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def rule(
        self, ruling: dict[str, Any], finding: dict[str, Any] | None = None, replays: list[str] | None = None
    ) -> tuple[dict[str, Any], dict[str, str]]:
        f = finding or lane_finding()
        base = {
            "finding_ref": "C1",
            "counter_test": None,
            "expectation_source": "intent",
            "severity_assessment": "agree",
            "reason": "r",
            "citations": [],
        }
        notes = verify.apply_rulings(
            {"C1": f}, {"rulings": [{**base, **ruling}]}, self.roots, {"model": "m", "call_id": "v1"}, replays or []
        )
        return f["verification"], notes

    def test_evidence_bar(self) -> None:
        good = [{"side": "head", "file": "pkg/ranges.py", "line": 4, "quote": "if value >= high:"}]
        off = [{"side": "head", "file": "pkg/ranges.py", "line": 10, "quote": "return x * 2"}]
        bad = [{"side": "head", "file": "pkg/ranges.py", "line": 4, "quote": "if value > high + 7:"}]
        introduced = lane_finding(replay={"head": {}, "base": {}, "classification": "introduced", "reason": ""})
        cases = [
            self.rule({"verdict": "CONFIRMED", "evidence_kind": "citation_chain", "citations": good}),
            self.rule({"verdict": "CONFIRMED", "evidence_kind": "citation_chain", "citations": off}),
            self.rule({"verdict": "CONFIRMED", "evidence_kind": "none"}),
            self.rule({"verdict": "CONFIRMED", "evidence_kind": "citation_chain", "citations": bad}),
            self.rule({"verdict": "CONFIRMED", "evidence_kind": "executed"}),
            self.rule({"verdict": "CONFIRMED", "evidence_kind": "executed"}, introduced),
            self.rule({"verdict": "REFUTED", "evidence_kind": "file_line", "citations": off}),
            self.rule({"verdict": "REFUTED", "evidence_kind": "counter_test"}),
            self.rule(
                {"verdict": "REFUTED", "evidence_kind": "counter_test"}, replays=["/r/replays/lanes/v1/001/record.json"]
            ),
            self.rule({"verdict": "UNSUBSTANTIATED", "evidence_kind": "none"}),
        ]
        self.assertEqual(
            [(v["verdict"], v["evidence"]["kind"], bool(n)) for v, n in cases],
            [
                ("CONFIRMED", "citation_chain", False),
                ("CONFIRMED", "citation_chain", True),
                ("UNSUBSTANTIATED", "none", True),
                ("UNSUBSTANTIATED", "none", True),
                ("UNSUBSTANTIATED", "none", True),
                ("CONFIRMED", "executed", False),
                ("REFUTED", "file_line", False),
                ("UNSUBSTANTIATED", "none", True),
                ("REFUTED", "counter_test", False),
                ("UNSUBSTANTIATED", "none", False),
            ],
        )

    def test_selection_batches_and_bare_claims(self) -> None:
        found = [
            lane_finding("critical", title="critical one", file="b.py"),
            lane_finding("major", title="major one", file="a.py"),
            lane_finding(
                "major", title="m-pre", replay={"head": {}, "base": {}, "classification": "pre-existing", "reason": ""}
            ),
            *[lane_finding("minor", title=f"minor-{i}", line=i) for i in range(1, 8)],
            lane_finding("nit", title="nit one"),
        ]
        chosen, skipped = verify.select(found, verify_cap=5)
        groups = verify.batches(chosen)
        text = verify.claim_text("C1", found[0])
        self.assertEqual(
            (
                sorted(f["title"] for f in chosen if f["severity"] != "minor"),
                len([f for f in chosen if f["severity"] == "minor"]),
                len(skipped),
                list(groups),
                [
                    s
                    for s in ("SECRET-FIX-TEXT", "SECRET-OBSERVED-TEXT", "SECRET-CHAIN-QUOTE", "claude-opus", "breaker")
                    if s in text
                ],
            ),
            (["critical one", "major one"], 5, 2, ["a.py", "b.py", "pkg/ranges.py"], []),
        )


class SynthesisTest(unittest.TestCase):
    def ver(self, verdict: str) -> dict[str, Any]:
        return {
            "verdict": verdict,
            "by": {"model": "m", "call_id": "v"},
            "evidence": {"kind": "none", "citations": [], "executed_ref": None},
            "expectation_source": "intent",
            "severity_assessment": "agree",
        }

    def test_buckets_and_confidence(self) -> None:
        introduced = {"head": {}, "base": {}, "classification": "introduced", "reason": ""}
        pre = {"head": {}, "base": {}, "classification": "pre-existing", "reason": ""}
        gate = fmod.make(
            origin="gate",
            category="typing",
            severity="major",
            title="New mypy error",
            file="a.py",
            start_line=1,
            conditions="c",
            expected="e",
            actual="a",
        )
        lane = [
            lane_finding(title="confirmed+replay", verification=self.ver("CONFIRMED"), replay=introduced),
            lane_finding(title="confirmed", line=11, verification=self.ver("CONFIRMED")),
            lane_finding(title="unsubstantiated", line=12, verification=self.ver("UNSUBSTANTIATED")),
            lane_finding(title="below bar", line=13, level="reasoned", verification=self.ver("UNSUBSTANTIATED")),
            lane_finding(title="refuted", line=14, verification=self.ver("REFUTED")),
            lane_finding(title="refuted but replayed", line=15, verification=self.ver("REFUTED"), replay=introduced),
            lane_finding(title="never verified", line=16),
            lane_finding(title="pre-existing", line=17, replay=pre),
            lane_finding("minor", title="minor confirmed", line=18, verification=self.ver("CONFIRMED")),
        ]
        below = [f["id"] for f in lane if f["title"] == "below bar"]
        syn = synthesize.synthesize([gate], lane, [], below, {})
        got = {f["title"]: (f["confidence"], f["bucket"], f["blocking"]) for f in syn.findings}
        self.assertEqual(
            (got, [f["title"] for f in syn.findings if f["id"] in syn.unverified_major]),
            (
                {
                    "New mypy error": ("certain", "blocking", True),
                    "confirmed+replay": ("certain", "blocking", True),
                    "confirmed": ("high", "blocking", True),
                    "unsubstantiated": ("medium", "must-address", False),
                    "below bar": ("medium", "non-blocking", False),
                    "refuted": ("low", "non-blocking", False),
                    "refuted but replayed": ("low", "contested", False),
                    "never verified": ("unverified", "non-blocking", False),
                    "pre-existing": ("unverified", "pre-existing", False),
                    "minor confirmed": ("high", "non-blocking", False),
                },
                ["never verified"],
            ),
        )


class VerdictRulesTest(unittest.TestCase):
    def found(self, bucket: str, severity: str = "major") -> list[dict[str, Any]]:
        f = lane_finding(severity)
        f["bucket"] = bucket
        return [f]

    def test_rules_in_order(self) -> None:
        esc = [{"condition": "degraded-lane", "detail": "x"}]
        d = verdict_mod.decide
        cases = [
            d("no-fix", esc, self.found("blocking"), 1.0, True, []),
            d("no-fix", [], self.found("blocking"), 1.0, True, []),
            d("loop", [], self.found("must-address"), 1.0, True, []),
            d("no-fix", [], self.found("contested", "critical"), 1.0, True, []),
            d("no-fix", [], self.found("contested", "major"), 1.0, True, []),
            d("no-fix", [], [], 0.9, True, []),
            d("no-fix", [], [], 1.0, False, []),
            d("no-fix", [], [], 1.0, True, ["r1-security-u1"]),
            d("no-fix", [], self.found("non-blocking"), 1.0, True, []),
        ]
        self.assertEqual(
            [(c.verdict, [e["condition"] for e in c.escalations]) for c in cases],
            [
                ("ESCALATE", ["degraded-lane"]),
                ("REQUEST_CHANGES", []),
                ("AUTHOR", []),
                ("ESCALATE", ["approve-condition:no-contested-critical"]),
                ("APPROVE", []),
                ("ESCALATE", ["approve-condition:coverage"]),
                ("ESCALATE", ["approve-condition:lanes-succeeded"]),
                ("ESCALATE", ["approve-condition:protected-lanes"]),
                ("APPROVE", []),
            ],
        )


class ReportTest(unittest.TestCase):
    def test_report_is_deterministic(self) -> None:
        found = [lane_finding("major", title=f"f{i}", line=i) for i in range(1, 6)]
        for i, f in enumerate(found):
            f["bucket"] = ["blocking", "must-address", "contested", "non-blocking", "pre-existing"][i]
        verdict = {
            "run_id": "r", "round": 1, "verdict": "REQUEST_CHANGES", "reason": "1 blocking", "escalations": [],
            "gate_summary": {"mypy": {"base": "ok", "head": "ok", "new": 0}}, "lanes": [], "mode": "no-fix",
            "coverage": {"changed_lines": 4, "excluded_lines": 0, "reviewed_lines": 4, "ratio": 1.0},
            "cost": {"total_usd": 1.0, "budget_usd": 15.0, "by_kind": {"lane": 1.0}}, "base_ref": "origin/master",
            "merge_base": "a" * 40, "snapshot_tree": "b" * 40, "snapshot_commit": "c" * 40, "single_family": True,
            "replay_isolation": "docker", "integrity": {"ok": True, "violations": []}, "stale_base": None,
        }  # fmt: skip
        store = {"final": sorted(found, key=fmod.sort_key), "rejected": [{"lane": "x", "reason": "quote"}]}
        shuffled = copy.deepcopy(store)
        random.Random(3).shuffle(shuffled["rejected"])
        first = report.render(verdict, store, {}, "intent")
        self.assertEqual(
            (
                first == report.render(verdict, copy.deepcopy(store), {}, "intent"),
                "## Blocking (1)" in first,
                "SECRET-FIX-TEXT" in first,
            ),
            (True, True, True),
        )


class DriverVerdictTest(DriverCase):
    """End to end through the driver: the verifier's ruling decides the verdict."""

    def outcome(self, plan: dict[str, str]) -> tuple[str, dict[str, Any], list[str]]:
        data = self.tmp / "breaker-findings.json"
        data.write_text(json.dumps({"findings": [wire_finding()], "notes": None}))
        run_dir, action = self.start({"breaker": f"findings:{data}", **plan, "*": "ok"})
        run = Run.load(run_dir)
        verdict = read_json(run_dir / "verdict.json")
        problems = validate(verdict, read_json(SCHEMAS_DIR / "verdict.schema.json"))
        return action["verdict"], verdict, problems + [e["condition"] for e in run.state["escalations"]]

    def test_verifier_ruling_decides(self) -> None:
        confirm = self.outcome({"verifier": "verify-confirm"})
        self.tearDown()
        self.setUp()
        refute = self.outcome({"verifier": "verify-refute"})
        self.tearDown()
        self.setUp()
        crash = self.outcome({"verifier": "crash"})
        self.assertEqual(
            [
                (confirm[0], len(confirm[1]["blocking_ids"]), confirm[2]),
                (refute[0], len(refute[1]["blocking_ids"]), refute[2]),
                (crash[0], crash[2]),
            ],
            [("REQUEST_CHANGES", 1, []), ("APPROVE", 0, []), ("ESCALATE", ["unverified-critical-major"])],
        )

    def test_identical_inputs_give_identical_reports(self) -> None:
        reports = []
        for _ in range(2):
            data = self.tmp / "breaker-findings.json"
            data.write_text(json.dumps({"findings": [wire_finding()], "notes": None}))
            run_dir, _action = self.start({"breaker": f"findings:{data}", "verifier": "verify-confirm", "*": "ok"})
            run_id = run_dir.name
            reports.append((run_dir / "report.md").read_text().replace(run_id, "RUN"))
            self.tearDown()
            self.setUp()
        self.assertEqual((reports[0] == reports[1], "## Blocking (1)" in reports[0]), (True, True))


if __name__ == "__main__":
    unittest.main()
