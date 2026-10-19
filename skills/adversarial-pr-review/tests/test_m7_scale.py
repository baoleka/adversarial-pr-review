"""M7: partitioning, the interface pass, the review-line cap, the budget and its drop order, and
priced telemetry."""

from __future__ import annotations

import json
import random
from pathlib import Path
from typing import Any

from support import FixtureRepo

# isort: split

import diffs
import interface
import phases_lanes
import telemetry
from engine import Ctx
from test_m3_lanes import CONFIG, DriverCase, TmpCase
from triage import partition

import config as config_mod
from common import read_json
from state import Run

CFG = config_mod.merge(config_mod.DEFAULTS, CONFIG)


def fd(path: str, lines: int) -> diffs.FileDiff:
    return diffs.FileDiff(path, path, "modified", added={i: "x" for i in range(1, lines + 1)})


class PartitionTest(TmpCase):
    def test_deterministic_tests_with_sources_and_size_cap(self) -> None:
        file_diffs = {
            "src/pkg/core.py": fd("src/pkg/core.py", 120),
            "tests/test_core.py": fd("tests/test_core.py", 40),
            "src/pkg/big_a.py": fd("src/pkg/big_a.py", 300),
            "src/pkg/big_b.py": fd("src/pkg/big_b.py", 300),
            "src/other/util.py": fd("src/other/util.py", 10),
        }
        paths = sorted(file_diffs)
        risk = {p: 1.0 for p in paths}
        first = [(u.id, u.files) for u in partition(file_diffs, paths, 400, risk)]
        shuffled = paths[:]
        random.Random(7).shuffle(shuffled)
        second = [(u.id, u.files) for u in partition(file_diffs, shuffled, 400, risk)]
        core_unit = next(files for _id, files in first if "src/pkg/core.py" in files)
        self.assertEqual(
            (
                first == second,
                "tests/test_core.py" in core_unit,
                any(set(files) >= {"src/pkg/big_a.py", "src/pkg/big_b.py"} for _id, files in first),
            ),
            (True, True, False),
        )


class InterfaceTest(TmpCase):
    def test_callers_outside_the_diff_are_found(self) -> None:
        repo = FixtureRepo.create(
            self.tmp,
            {
                "pkg/a.py": "def f(x):\n    return x\n",
                "pkg/b.py": "from pkg.a import f\n\nVALUE = f(1)\n",
                "pkg/c.py": "y = 2\n",
            },
        )
        base = repo.git("rev-parse", "HEAD").strip()
        repo.write("pkg/a.py", "def f(x, y):\n    return x + y\n")
        repo.write("pkg/c.py", "y = 3\n")
        head = repo.commit("change f")
        text, files = interface.summary(repo.root, base, head, ["pkg/a.py:f (changed)"], ["pkg/a.py", "pkg/c.py"])
        self.assertEqual(
            (files, "`pkg/b.py:3: VALUE = f(1)`" in text, "- base: `(x)`" in text, "- head: `(x, y)`" in text),
            (["pkg/b.py"], True, True, True),
        )


class BudgetTest(TmpCase):
    def ctx(self, budget: float) -> Ctx:
        run_dir = self.tmp / f"run-{budget}"
        run_dir.mkdir()
        run = Run(run_dir)
        run.state = {
            "mode": "no-fix",
            "args": {"budget_usd": budget},
            "calls": [],
            "escalations": [],
            "round": 1,
            "phase": "LANES",
            "rounds": {"1": {}},
        }
        return Ctx(run, self.tmp)

    def entries(self, kinds: dict[str, float]) -> dict[str, Any]:
        return {
            f"r1-{k}-u1": {"spec": {"id": f"r1-{k}-u1", "kind": k, "estimate_usd": est}, "status": "pending"}
            for k, est in kinds.items()
        }

    def test_drop_order(self) -> None:
        kinds = {"breaker": 1.0, "external": 0.4, "contracts": 0.8, "tests": 0.35, "security": 0.8}
        reserve = 2 * telemetry.estimate_call(CFG, "verifier", "claude-opus-5-5", "xhigh", 8000)
        results = {}
        triage = {"sensitive_files": ["a.py"], "security_hits": [], "public_interface_changes": []}
        for budget in (10.0, reserve + 3.0, reserve + 2.2, reserve + 1.85, reserve + 1.0):
            ctx = self.ctx(budget)
            entries = self.entries(kinds)
            phases_lanes._plan_budget(ctx, CFG, entries, triage)
            kept = sorted(e["spec"]["kind"] for e in entries.values() if e["status"] == "pending")
            results[round(budget - reserve, 2)] = (kept, [e["condition"] for e in ctx.state["escalations"]])
        self.assertEqual(
            results,
            {
                round(10.0 - reserve, 2): (["breaker", "contracts", "external", "security", "tests"], []),
                3.0: (["breaker", "contracts", "external", "security"], []),
                2.2: (["breaker", "external", "security"], []),
                1.85: (["breaker", "security"], []),
                1.0: ([], ["budget"]),
            },
        )

    def test_pricing(self) -> None:
        opus = telemetry.estimate_call(CFG, "breaker", "claude-opus-5-5", "xhigh", 10_000)
        unknown = telemetry.estimate_call(CFG, "breaker", "some-other-model", "xhigh", 10_000)
        fable = telemetry.estimate_call(CFG, "breaker", "claude-fable-5-1", "xhigh", 10_000)
        usage = telemetry.usage_from_result(
            {
                "total_cost_usd": 0.42,
                "usage": {
                    "input_tokens": 3,
                    "output_tokens": 4,
                    "cache_read_input_tokens": 5,
                    "cache_creation_input_tokens": 6,
                },
                "num_turns": 2,
            }
        )
        self.assertEqual(
            (0 < opus < fable, unknown == fable, usage["cost_usd"], usage["cache_read"], usage["turns"]),
            (True, True, 0.42, 5, 2),
        )


class ScaleDriverTest(DriverCase):
    FILES = {
        "mod1/a.py": "def f(x):\n    return x\n",
        "mod2/b.py": "def g(x):\n    return x\n",
        "mod3/c.py": "from mod1.a import f\n\nVALUE = f(1)\n",
    }
    EDITS = {
        "mod1/a.py": "def f(x, y=0):\n" + "".join(f"    v{i} = x + {i}\n" for i in range(30)) + "    return x + y\n",
        "mod2/b.py": "def g(x):\n" + "".join(f"    w{i} = x * {i}\n" for i in range(30)) + "    return x\n",
    }

    def with_config(self, config: dict[str, Any], **kwargs: Any) -> tuple[Path, dict[str, Any]]:
        backup = json.loads(json.dumps(CONFIG))
        CONFIG.clear()
        CONFIG.update(config)
        try:
            return self.start({"*": "ok"}, **kwargs)
        finally:
            CONFIG.clear()
            CONFIG.update(backup)

    def test_interface_pass_and_review_line_cap(self) -> None:
        config = json.loads(json.dumps(CONFIG))
        config["repo"]["file_classes"] = {"public_interface": ["mod*/*.py"]}
        config["limits"] = {"unit_size_cap": 20}
        run_dir, action = self.with_config(config, files=self.FILES, edits=self.EDITS, args=("--no-fix", "grow"))
        run = Run.load(run_dir)
        lanes_run = sorted(run.state["rounds"]["1"]["lanes"])
        brief = (run_dir / "lanes" / "r1-interface-i1" / "brief.md").read_text()
        self.tearDown()
        self.setUp()
        config["limits"] = {"unit_size_cap": 20, "max_review_lines": 20}
        capped_dir, capped = self.with_config(config, files=self.FILES, edits=self.EDITS, args=("--no-fix", "grow"))
        capped_run = Run.load(capped_dir)
        self.assertEqual(
            (
                "r1-interface-i1" in lanes_run,
                "`mod3/c.py:3: VALUE = f(1)`" in brief,
                action["verdict"],
                capped["verdict"],
                [e["condition"] for e in capped_run.state["escalations"]],
                read_json(capped_dir / "verdict.json")["coverage"]["ratio"] < 1,
            ),
            (True, True, "APPROVE", "ESCALATE", ["coverage<100"], True),
        )

    def test_no_call_starts_over_budget(self) -> None:
        run_dir, action = self.start({"*": "ok"}, args=("--no-fix", "--budget", "0.5", "x"))
        run = Run.load(run_dir)
        self.assertEqual(
            (
                action["verdict"],
                sorted({e["condition"] for e in run.state["escalations"]}),
                [c["kind"] for c in run.state.get("calls", [])],
            ),
            ("ESCALATE", ["budget"], ["probe"]),
        )


if __name__ == "__main__":
    import unittest

    unittest.main()
