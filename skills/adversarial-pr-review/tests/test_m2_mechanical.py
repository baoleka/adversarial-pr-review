import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from support import SKILL_DIR, FixtureRepo

# isort: split

import checks
import diffs
import gates
import parsers
from parsers import Diagnostic
from triage import ast_equal_without_docstrings, change_types, run_triage

import config as config_mod

STANDIN_CONFIG = json.loads((SKILL_DIR / "tests" / "fixtures" / "standin.review.config.json").read_text())
CFG = config_mod.merge(config_mod.DEFAULTS, STANDIN_CONFIG)
VENV_BIN = Path.home() / ".cache" / "adversarial-review" / "venvs" / "standin-py3.12" / "bin"
ITEMS = Path.home() / ".cache" / "adversarial-review" / "evals" / "items"


def d(tool="mypy", rule="arg-type", file="a.py", line=1, message="bad", symbol="f"):
    return Diagnostic(tool, rule, file, line, message, symbol)


class ParserTest(unittest.TestCase):
    def test_ruff_json(self):
        root = Path("/repo")
        text = json.dumps(
            [
                {
                    "code": "E722",
                    "message": "Do not use bare `except`",
                    "filename": "/repo/src/x.py",
                    "location": {"row": 7, "column": 1},
                }
            ]
        )
        self.assertEqual(
            parsers.parse_ruff_json(text, root), [Diagnostic("ruff", "E722", "src/x.py", 7, "Do not use bare `except`")]
        )

    def test_mypy_json_keeps_errors_only(self):
        text = "\n".join(
            json.dumps(x)
            for x in [
                {
                    "file": "src/x.py",
                    "line": 3,
                    "column": 0,
                    "message": "Function is missing a type annotation",
                    "hint": None,
                    "code": "no-untyped-def",
                    "severity": "error",
                },
                {
                    "file": "src/x.py",
                    "line": 3,
                    "column": 0,
                    "message": "See docs",
                    "hint": None,
                    "code": None,
                    "severity": "note",
                },
            ]
        )
        self.assertEqual([x.rule for x in parsers.parse_mypy_json(text, Path("/"))], ["no-untyped-def"])

    def test_untyped_line_and_file_fail_closed(self):
        bad_file = json.dumps({"file": 3, "line": "12", "message": "x", "code": "y", "severity": "error"})
        with self.assertRaises(ValueError):
            parsers.parse_mypy_json(bad_file, Path("/"))
        weird_line = json.dumps({"file": "a.py", "line": "(20", "message": "x", "code": "y", "severity": "error"})
        self.assertEqual(parsers.parse_mypy_json(weird_line, Path("/"))[0].line, 1)

    def test_masking_is_linear_on_hostile_input(self):
        payload = "([a-z]+)+~" * 5000 + "a" * 100000
        start = time.time()
        parsers.mask(payload)
        parsers.parse_mypy_text(payload, Path("/"))
        self.assertLess(time.time() - start, 1.0)

    def test_mask_hides_numbers_and_paths(self):
        self.assertEqual(
            parsers.mask('Argument 1 to "f" has incompatible type; see /tmp/x/y.py line 12 at 0x7fff'),
            'Argument <n> to "f" has incompatible type; see <path> line <n> at <hex>',
        )

    def test_pytest_report(self):
        text = json.dumps(
            {
                "collected": ["t.py::a", "t.py::b"],
                "outcomes": {
                    "t.py::a": {"outcome": "failed", "file": "t.py", "line": 3, "message": "AssertionError"},
                    "t.py::b": {"outcome": "passed", "file": "t.py", "line": 9, "message": ""},
                },
                "exitstatus": 1,
            }
        )
        report = parsers.parse_pytest_report(text)
        self.assertEqual([x.message for x in parsers.pytest_failures(report)], ["t.py::a: AssertionError"])

    def test_enclosing_symbol(self):
        spans = parsers.symbol_map("class A:\n    def m(self):\n        x = 1\n\ndef f():\n    pass\n")
        self.assertEqual([parsers.enclosing_symbol(spans, n) for n in (1, 3, 6, 4)], ["A", "A.m", "f", "<module>"])


class DiagnosticDiffTest(unittest.TestCase):
    def test_multiset_excess_changed_fixed_suppressed(self):
        base = [
            d(line=10),
            d(line=20),
            d(rule="x", message="old text"),
            d(rule="gone", line=30),
            d(rule="silenced", line=40),
        ]
        head = [d(line=11), d(line=21), d(line=50), d(rule="x", message="new text")]

        def anchor(diag):
            return {30: "changed", 40: "suppression"}.get(diag.line, "unchanged")

        out = parsers.diff_diagnostics(base, head, anchor)
        self.assertEqual(
            (
                [x.line for x in out.new],
                len(out.changed),
                [x.rule for x in out.fixed],
                [x.rule for x in out.suppressed],
                len(out.persisting),
            ),
            ([50], 1, ["gone"], ["silenced"], 2),
        )

    def test_anchor_classification_from_diff(self):
        text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -3 +3 @@\n-x = f(1)\n+x = f(1)  # type: ignore[arg-type]\n@@ -9 +9 @@\n-y = 2\n+y = 3\n"
        classify = parsers.anchor_change_from_diff(diffs.parse(text))
        self.assertEqual([classify(d(line=n)) for n in (3, 9, 5)], ["suppression", "changed", "unchanged"])


class GateClassificationTest(unittest.TestCase):
    def gate(self, parser="mypy", count=True):
        g = {"name": "mypy", "argv": ["mypy"], "parser": parser, "timeout_s": 60}
        if count:
            g["error_count_gate"] = {"baseline_file": ".count", "op": "<="}
        return g

    def test_new_error_within_count_limit_is_minor(self):
        base = gates.SideResult("mypy", "base", "ok", 0, [], error_count=5)
        head = gates.SideResult("mypy", "head", "ok", 0, [d(line=3)], error_count=6)
        diff = parsers.diff_diagnostics([], [d(line=3)], lambda x: "unchanged")
        out = gates.gate_findings(self.gate(), base, head, diff, {}, 1, limit=8, exclude_nodes=set())
        self.assertEqual(
            (len(out), out[0]["severity"], out[0]["origin"], out[0]["confidence"]), (1, "minor", "gate", "certain")
        )

    def test_new_error_over_the_count_gate_is_major(self):
        base = gates.SideResult("mypy", "base", "ok", 0, [], error_count=5)
        head = gates.SideResult("mypy", "head", "failed", 1, [d(line=3)], error_count=6)
        diff = parsers.diff_diagnostics([], [d(line=3)], lambda x: "unchanged")
        out = gates.gate_findings(self.gate(), base, head, diff, {}, 1, limit=5, exclude_nodes=set())
        self.assertEqual(out[0]["severity"], "major")

    def test_flaky_classification(self):
        self.assertEqual(
            [
                gates.classify_flaky(["failed", "failed"], ["passed", "failed"]),
                gates.classify_flaky(["failed", "failed", "failed"], ["passed"] * 3),
                gates.classify_flaky(["failed", "passed"], ["passed"] * 3),
            ],
            ["flaky", "regression", "intermittent"],
        )

    def test_base_cache_key_changes_with_toolchain_and_tree(self):
        g = self.gate()
        keys = {
            gates.base_cache_key(g, "tree1", "tool1", CFG),
            gates.base_cache_key(g, "tree2", "tool1", CFG),
            gates.base_cache_key(g, "tree1", "tool2", CFG),
        }
        self.assertEqual(len(keys), 3)


class GlobTest(unittest.TestCase):
    def test_glob_semantics(self):
        from common import glob_match

        cases = [
            ("src/click/a.py", "src/click/**/*.py", True),
            ("src/click/sub/a.py", "src/click/**/*.py", True),
            ("src/clickx/a.py", "src/click/**", False),
            ("docs/guide/x.md", "*.md", True),
            ("README.md", "*.md", True),
            ("a/.env.local", "**/.env*", True),
            ("requirements/dev.txt", "requirements/*.txt", True),
            ("requirements/sub/dev.txt", "requirements/*.txt", False),
            (".github/workflows/ci.yml", ".github/workflows/**", True),
        ]
        self.assertEqual([glob_match(p, [g]) for p, g, _ in cases], [want for _, _, want in cases])


class CheckTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adrev-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_suppressions_only_on_added_lines(self):
        text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -1 +1,3 @@\n-x = 1\n+x = 1  # type: ignore\n+y = 2  # noqa\n+z = 3  # noqa: E501\n"
        out = checks.added_line_suppressions(diffs.parse(text), CFG, 1)
        self.assertEqual(
            sorted((f["location"]["start_line"], f["severity"]) for f in out), [(1, "major"), (2, "major")]
        )

    def test_tool_suppression_findings_filter_to_added_lines(self):
        text = "diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -5 +5 @@\n-x = 1\n+x = 1  # type: ignore\n"
        diags = [
            Diagnostic("ruff", "PGH003", "a.py", 5, "Use specific rule codes"),
            Diagnostic("ruff", "PGH003", "a.py", 9, "old one"),
            Diagnostic("ruff", "E501", "a.py", 5, "long"),
        ]
        out = checks.tool_suppression_findings(diags, diffs.parse(text), CFG, 1)
        self.assertEqual([f["location"]["start_line"] for f in out], [5])

    def test_changelog_rules(self):
        repo = FixtureRepo.create(
            self.tmp,
            {
                "src/click/a.py": "x = 1\n",
                "CHANGELOG.md": "# Changelog\n\n## Unreleased\n\n## 1.0\n\n- old\n",
                "RD-CHANGELOG.md": "# R&D\n\n## Unreleased\n",
            },
        )
        base = repo.git("rev-parse", "HEAD").strip()
        repo.write("src/click/a.py", "x = 2\n")
        head = repo.commit("change")
        missing = checks.changelog_findings(
            repo.root, CFG, base, diffs.diff_between(repo.root, base, head), "change", "", 1
        )
        repo.write("CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n## 1.0\n\n- old\n- misplaced entry\n")
        head2 = repo.commit("misplaced")
        misplaced = checks.changelog_findings(
            repo.root, CFG, base, diffs.diff_between(repo.root, base, head2), "", "", 1
        )
        repo.write("CHANGELOG.md", "# Changelog\n\n## Unreleased\n\n- good entry\n\n## 1.0\n\n- old\n")
        head3 = repo.commit("good")
        good = checks.changelog_findings(repo.root, CFG, base, diffs.diff_between(repo.root, base, head3), "", "", 1)
        skipped = checks.changelog_findings(
            repo.root, CFG, base, diffs.diff_between(repo.root, base, head), "fix\n\nchangelog: skip\n", "", 1
        )
        self.assertEqual(
            ([f["severity"] for f in missing], [f["severity"] for f in misplaced], good, skipped),
            (["major"], ["minor"], [], []),
        )

    def test_tests_run_check(self):
        root = self.tmp / "wt"
        (root / "tests").mkdir(parents=True)
        source = "import pytest\n\ndef test_ok():\n    assert 1\n\ndef check_never_test():\n    assert 1\n\n@pytest.mark.skip\ndef test_skipped():\n    assert 1\n\ndef test_param():\n    assert 1\n"
        (root / "tests" / "test_x.py").write_text(source)
        diff_text = (
            "diff --git a/tests/test_x.py b/tests/test_x.py\n--- a/tests/test_x.py\n+++ b/tests/test_x.py\n@@ -0,0 +1,14 @@\n"
            + "".join(f"+{line}\n" for line in source.splitlines())
        )
        report = parsers.PytestReport(
            ["tests/test_x.py::test_ok", "tests/test_x.py::test_skipped", "tests/test_x.py::test_param[1]"],
            {
                "tests/test_x.py::test_ok": {"outcome": "passed"},
                "tests/test_x.py::test_skipped": {"outcome": "skipped"},
                "tests/test_x.py::test_param[1]": {"outcome": "passed"},
            },
            0,
        )
        out = checks.tests_run_findings(CFG, root, diffs.parse(diff_text), report, 1)
        self.assertEqual(
            sorted((f["title"], f["severity"]) for f in out),
            sorted(
                [
                    ("Test `check_never_test` is never collected", "major"),
                    ("Test `test_skipped` is collected but never runs", "major"),
                    ("New skip or xfail marker", "minor"),
                ]
            ),
        )

    def test_diff_coverage(self):
        root = self.tmp / "wt"
        (root / "src").mkdir(parents=True)
        (root / "src" / "m.py").write_text("def f(x):\n    if x:\n        return 1\n    return 2\n")
        xml = self.tmp / "coverage.xml"
        xml.write_text(
            f'<coverage><sources><source>{root}</source></sources><packages><package><classes><class filename="src/m.py"><lines><line number="2" hits="1"/><line number="3" hits="1"/><line number="4" hits="0"/></lines></class></classes></package></packages></coverage>'
        )
        diff_text = "diff --git a/src/m.py b/src/m.py\n--- a/src/m.py\n+++ b/src/m.py\n@@ -2,3 +2,3 @@\n-x\n-y\n-z\n+    if x:\n+        return 1\n+    return 2\n"
        finding, summary = checks.diff_coverage_finding(CFG, root, diffs.parse(diff_text), str(xml), 1)
        self.assertEqual(
            (summary["covered"], summary["changed_executable_lines"], finding["severity"]), (2, 3, "minor")
        )


class TriageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="adrev-test-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_docstring_and_comment_changes_are_ast_equal(self):
        old = 'def f():\n    """Doc."""\n    return 1  # a\n'
        new = 'def f():\n    """Better doc."""\n    # explain\n    return 1\n'
        self.assertEqual(
            (ast_equal_without_docstrings(old, new), ast_equal_without_docstrings(old, new.replace("1", "2"))),
            (True, False),
        )

    def test_change_types_from_intent(self):
        self.assertEqual(
            (change_types("refactor: simplify X"), change_types("whatever")),
            (["refactor"], ["dependency", "feature", "fix", "refactor"]),
        )

    def test_lane_selection_by_size_interface_and_sensitivity(self):
        repo = FixtureRepo.create(
            self.tmp,
            {
                "src/click/a.py": "def f():\n    return 1\n",
                "src/click/utils.py": "def g():\n    return 1\n",
                "docs/x.md": "doc\n",
            },
        )

        def lanes_for(files: dict[str, str]) -> list[str]:
            base = repo.git("rev-parse", "HEAD").strip()
            for rel, text in files.items():
                repo.write(rel, text)
            head = repo.commit("c")
            out = run_triage(repo.root, CFG, base, head, diffs.diff_between(repo.root, base, head), "fix: x")
            return out.lanes if not out.comment_only else ["(no lanes)"]

        small = lanes_for({"src/click/a.py": "def f():\n    return 2\n"})
        iface = lanes_for({"src/click/a.py": "def f(x=1):\n    return 2\n"})
        sensitive = lanes_for({"src/click/utils.py": "def g():\n    return 2\n"})
        docs_only = lanes_for({"docs/x.md": "doc 2\n"})
        self.assertEqual(
            (small, iface, sensitive[:2], "security" in sensitive, docs_only),
            (
                ["breaker", "external"],
                ["breaker", "external", "contracts"],
                ["breaker", "external"],
                True,
                ["(no lanes)"],
            ),
        )


@unittest.skipUnless(
    (ITEMS / "P07").is_dir() and (VENV_BIN / "mypy").exists(), "benchmark items or stand-in toolchain missing"
)
class MechanicalIntegrationTest(unittest.TestCase):
    def test_typecheck_count_gate_item(self):
        tmp = Path(tempfile.mkdtemp(prefix="adrev-int-"))
        try:
            repo = tmp / "P07"
            shutil.copytree(ITEMS / "P07", repo, symlinks=True)
            env = dict(os.environ, PATH=f"{VENV_BIN}{os.pathsep}{os.environ['PATH']}")
            review = [sys.executable, "-E", "-s", str(SKILL_DIR / "scripts" / "review.py")]
            out = json.loads(
                subprocess.run(
                    [*review, "start", "--no-fix", "feat: add indent_lines"],
                    cwd=repo,
                    env=env,
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
            while out["action"]["kind"] in ("exec", "wait"):
                out = json.loads(
                    subprocess.run(
                        out["action"]["argv"], cwd=repo, env=env, capture_output=True, text=True, check=True
                    ).stdout
                )
            data = json.loads((Path(out["run"]) / "findings.json").read_text())
            found = [(f["category"], f["severity"], f["location"]["file"]) for f in data["rounds"]["1"]["mechanical"]]
            self.assertIn(("typing", "major", "src/click/formatting.py"), found)
        finally:
            for root, dirs, _ in os.walk(tmp):
                for name in dirs:
                    os.chmod(os.path.join(root, name), 0o755)
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


class CostGateTest(unittest.TestCase):
    def test_estimates_follow_the_cost_model_and_budget_binds(self):
        import telemetry

        small = telemetry.estimate_call(CFG, "breaker", "claude-opus-5-5", "xhigh", 6000)
        fable = telemetry.estimate_call(CFG, "breaker", "claude-fable-5-1", "xhigh", 6000)
        unknown = telemetry.estimate_call(CFG, "breaker", "some-alias", "xhigh", 6000)
        state = {"mode": "no-fix", "args": {"budget_usd": None}, "calls": [{"cost_usd": 14.5}]}
        self.assertEqual(
            (
                0.6 < small < 0.85,  # the plan's 1.1-1.4 per breaker call, scaled by the 0.6 calibration
                fable > 2 * small,
                unknown >= fable,
                telemetry.can_afford(state, CFG, 0.4),
                telemetry.can_afford(state, CFG, 0.6),
            ),
            (True, True, True, True, False),
        )
