"""A verified regression introduced by the author's fix blocks at any severity (heldout2 follow-up):
the loop tags it, verification always covers it, synthesis blocks on it, and its fix must verify."""

from __future__ import annotations

import json
from typing import Any

import test_m5_loop as m5
from support import FixtureRepo

# isort: split

import diffs
import phases_loop
import phases_verify
import synthesize
import verify
from test_m3_lanes import CONFIG, TmpCase, wire_finding

from common import read_json

REGRESSING_FIX_SRC = m5.FIXED_SRC.replace(
    "def clamp(value, low, high):\n", "def clamp(value, low, high):\n    value = int(value)\n", 1
)
DELTA = {
    "pkg/ranges.py": diffs.FileDiff(
        "pkg/ranges.py",
        "pkg/ranges.py",
        "modified",
        added={2: "    value = int(value)", 3: "    assert clamp(1, 0, 2) == 1"},
    )
}
QUOTES = {
    2: "value = int(value)",
    3: "assert clamp(1, 0, 2) == 1",
    4: "if value >= high:",
    5: "return high - 1",
    6: "return value",
    8: "return x * 2",
}


def new_finding(category: str = "regression", line: int = 2, severity: str = "minor") -> dict[str, Any]:
    return m5.finding(
        bucket="non-blocking",
        severity=severity,
        category=category,
        title=f"A {category} finding on line {line}",
        quote=QUOTES[line],
        line=line,
        introduced_by=[{"side": "head", "file": "pkg/ranges.py", "line": line}],
    )


def verified(f: dict[str, Any], verdict: str) -> dict[str, Any]:
    f["verification"] = {
        "verdict": verdict,
        "by": {"model": "m", "call_id": "c"},
        "evidence": {"kind": "citation_chain", "citations": [], "executed_ref": None},
        "expectation_source": "intent",
        "severity_assessment": "agree",
    }
    return f


class TaggingTest(TmpCase):
    def test_only_new_behavior_defects_on_fix_lines_are_tagged(self) -> None:
        on_fix = new_finding()
        elsewhere = new_finding(line=8)
        test_quality = new_finding(category="test-quality", line=3)
        found = [on_fix, elsewhere, test_quality]
        merged, _notes = phases_loop.merge_new_findings([], found, DELTA)
        by_id = {f["id"]: f for f in merged}
        self.assertEqual(
            (
                [(bool(by_id[f["id"]].get("fix_regression")), bool(by_id[f["id"]].get("late"))) for f in found],
                phases_loop.fix_regression_ids(found, DELTA),
            ),
            ([(True, False), (False, True), (False, False)], [on_fix["id"]]),
        )


class VerificationTest(TmpCase):
    def test_fix_regressions_are_always_verified(self) -> None:
        minors = [new_finding(line=n) for n in (2, 3, 4)]
        nit = new_finding(line=5, severity="nit")
        chosen, skipped = verify.select([*minors, nit], 1, always={minors[2]["id"], nit["id"]})
        self.assertEqual(
            (sorted(f["id"] for f in chosen), [f["id"] for f in skipped]),
            (sorted([minors[0]["id"], minors[2]["id"], nit["id"]]), [minors[1]["id"]]),
        )


class SynthesisTest(TmpCase):
    def test_buckets(self) -> None:
        def outcome(f: dict[str, Any]) -> tuple[str, bool, int]:
            syn = synthesize.synthesize([], [f], [], [], {})
            return syn.findings[0]["bucket"], f["id"] in syn.unverified_major, len(syn.notes.get(f["id"], []))

        tagged = {"fix_regression": True}
        self.assertEqual(
            {
                "confirmed": outcome(verified({**new_finding(), **tagged}, "CONFIRMED")),
                "unsubstantiated": outcome(verified({**new_finding(), **tagged}, "UNSUBSTANTIATED")),
                "unverified": outcome({**new_finding(), **tagged}),
                "confirmed, untagged": outcome(verified(new_finding(), "CONFIRMED")),
            },
            {
                "confirmed": ("blocking", False, 1),
                "unsubstantiated": ("non-blocking", False, 1),
                "unverified": ("non-blocking", True, 1),
                "confirmed, untagged": ("non-blocking", False, 0),
            },
        )


class FixStaysBindingTest(TmpCase):
    def test_an_unverified_fix_of_a_fix_regression_is_not_deferred(self) -> None:
        tagged = {**new_finding(), "fix_regression": True, "bucket": "blocking"}
        plain = new_finding(line=6)
        fixes = {tagged["id"]: {"status": "not-fixed"}, plain["id"]: {"status": "not-fixed"}}
        answer = {"action": "fix", "note": "done", "diverges_from_suggestion": False, "reason": None, "evidence": []}
        answers = {tagged["id"]: answer, plain["id"]: answer}
        carried = {f["id"]: f["status"] for f in phases_loop.carry_over([tagged, plain], answers, fixes, {}, 3)}
        self.assertEqual(
            (carried, phases_verify.pending_fixes(fixes, [tagged, plain])),
            ({tagged["id"]: "not-fixed", plain["id"]: "deferred"}, [tagged["id"]]),
        )


class LoopEndToEndTest(TmpCase):
    """Round 1 finds the major bug. The author's fix adds `value = int(value)`, which round 2 reports
    as a minor finding on that line, and the verifier confirms it."""

    repo = m5.LoopTest.repo
    review = m5.LoopTest.review
    loop = m5.LoopTest.loop
    conditions = m5.LoopTest.conditions

    def run_loop(
        self, category: str, fix_the_regression: bool, config: dict[str, Any] | None = None
    ) -> tuple[Any, dict[str, Any], list[str]]:
        data = self.tmp / "round2-findings.json"
        regression = wire_finding(
            category=category,
            severity="minor",
            title="Floats are truncated to int before clamping",
            location={"start_line": 2, "end_line": 2, "quote": "value = int(value)"},
            introduced_by=[{"side": "head", "file": "pkg/ranges.py", "line": 2}],
            failure_scenario={"conditions": "clamp(2.5, 0, 10)", "expected": "2.5", "actual": "2"},
            evidence={
                "citation_chain": [{"side": "head", "file": "pkg/ranges.py", "line": 2, "quote": "value = int(value)"}]
            },
        )
        data.write_text(json.dumps({"findings": [regression], "notes": None}))
        plan = {"verifier": "verify-confirm", "r2-delta": f"findings:{data}", "r2-breaker": f"findings:{data}"}

        def author(repo: FixtureRepo, action: dict[str, Any], n: int) -> None:
            regressing = n == 1 or not fix_the_regression
            repo.write("pkg/ranges.py", REGRESSING_FIX_SRC if regressing else m5.FIXED_SRC)
            for item in action["findings"]:
                self.review(repo, "dispose", item["id"], "--action", "fix", "--note", "Fixed.")

        return self.loop(plan, author, config)

    def floats(self, run: Any, round_no: int) -> list[dict[str, Any]]:
        store = read_json(run.dir / "findings.json")
        return [f for f in store["rounds"][str(round_no)]["final"] if f["title"].startswith("Floats")]

    def test_a_confirmed_regression_of_the_fix_blocks_until_fixed(self) -> None:
        run, action, kinds = self.run_loop("regression", fix_the_regression=True)
        self.assertEqual(
            (
                action["verdict"],
                kinds,
                [(f["severity"], f["bucket"], bool(f.get("fix_regression"))) for f in self.floats(run, 2)],
                [(f["status"], f["bucket"]) for f in self.floats(run, 3)],
            ),
            ("APPROVE", ["author", "author", "done"], [("minor", "blocking", True)], [("fixed", "non-blocking")]),
        )

    def test_a_test_quality_finding_on_the_fix_does_not_block(self) -> None:
        run, action, kinds = self.run_loop("test-quality", fix_the_regression=False)
        self.assertEqual(
            (action["verdict"], kinds, [f["bucket"] for f in self.floats(run, 2)]),
            ("APPROVE", ["author", "done"], ["non-blocking"]),
        )

    def test_an_unfixed_regression_of_the_fix_ends_at_the_round_cap(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["limits"] = {"max_rounds": 2}
        run, action, kinds = self.run_loop("regression", fix_the_regression=False, config=config)
        self.assertEqual(
            (action["verdict"], kinds, self.conditions(run)), ("ESCALATE", ["author", "escalate"], ["round-cap"])
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
