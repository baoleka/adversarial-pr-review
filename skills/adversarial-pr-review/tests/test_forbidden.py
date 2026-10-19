"""Forbidden strings anywhere in the skill tree, and repo literals outside config in its code.

The evals tree has its own copy of this check (evals/tests/test_harness.py).
"""

from __future__ import annotations

import re
import unittest

from support import SKILL_DIR

FORBIDDEN = [
    r"dangerously",
    r"bypassPermissions",
    r"danger-full-access",
    r"yolo",
    r"approve-for-me",
    r"sandbox_mode",
    r"--trust-plugin",
    r"/dev/null",
    r"DEVNULL",
    r"(CODEX|ANTHROPIC|OPENAI)_API_KEY\s*=",
]
# Facts about one repo belong in review.config.json. Tests use the stand-in's config on purpose.
REPO_LITERALS = [r"AGENTS\.md", r"RD-CHANGELOG", r"\bclick\b", r"engineering:", r"\bsource_id\b"]
EXEMPT = {"tests/test_forbidden.py", "references/forbidden.md"}


def tree_files() -> list[tuple[str, str]]:
    files = []
    for path in sorted(SKILL_DIR.rglob("*")):
        rel = path.relative_to(SKILL_DIR).as_posix()
        if not path.is_file() or "__pycache__" in path.parts or rel in EXEMPT:
            continue
        files.append((rel, path.read_text(encoding="utf-8", errors="replace")))
    return files


class ForbiddenTest(unittest.TestCase):
    def offenders(self, patterns: list[str], include_tests: bool) -> list[str]:
        regex = re.compile("|".join(patterns))
        found = []
        for rel, text in tree_files():
            if not include_tests and rel.startswith("tests/"):
                continue
            for number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    found.append(f"{rel}:{number}: {line.strip()[:120]}")
        return found

    def test_no_forbidden_strings(self) -> None:
        self.assertEqual(self.offenders(FORBIDDEN, include_tests=True), [])

    def test_no_repo_literals_in_skill_code(self) -> None:
        self.assertEqual(self.offenders(REPO_LITERALS, include_tests=False), [])


if __name__ == "__main__":
    unittest.main()
