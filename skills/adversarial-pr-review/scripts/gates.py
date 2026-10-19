"""Run the repo's CI gates at base and head exactly as CI runs them, and diff their diagnostics.

Gate commands, tool configuration and error-count baselines come from the merge-base: the base
worktree is the merge-base, and any review-input file that differs at head is overlaid with its
merge-base copy before the head gates run (that difference is also a review-integrity finding).
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import findings as fmod
import parsers

from common import SCRIPTS_DIR, ReviewError, canonical_json, git, glob_match, read_json, run, sha256_text, write_json

PLUGIN_DIR = SCRIPTS_DIR / "pytest_plugin"
CATEGORY_BY_TOOL = {"ruff": "lint", "ruff-format": "format", "mypy": "typing", "pytest": "test-failure"}


@dataclass
class SideResult:
    gate: str
    side: str
    status: str  # ok | failed | unusable
    exit_code: int | None
    diagnostics: list[parsers.Diagnostic] = field(default_factory=list)
    pytest: parsers.PytestReport | None = None
    coverage_xml: str | None = None
    error_count: int | None = None
    problem: str | None = None
    cached: bool = False

    def to_json(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "side": self.side,
            "status": self.status,
            "exit_code": self.exit_code,
            "diagnostics": [d.to_json() for d in self.diagnostics],
            "pytest": None
            if self.pytest is None
            else {
                "collected": self.pytest.collected,
                "outcomes": self.pytest.outcomes,
                "exitstatus": self.pytest.exitstatus,
            },
            "coverage_xml": self.coverage_xml,
            "error_count": self.error_count,
            "problem": self.problem,
            "cached": self.cached,
        }

    @staticmethod
    def from_json(data: dict[str, Any]) -> SideResult:
        report = data.get("pytest")
        return SideResult(
            gate=data["gate"],
            side=data["side"],
            status=data["status"],
            exit_code=data["exit_code"],
            diagnostics=[
                parsers.Diagnostic(d["tool"], d["rule"], d["file"], d["line"], d["message"], d["symbol"])
                for d in data["diagnostics"]
            ],
            pytest=None
            if report is None
            else parsers.PytestReport(report["collected"], report["outcomes"], report["exitstatus"]),
            coverage_xml=data.get("coverage_xml"),
            error_count=data.get("error_count"),
            problem=data.get("problem"),
            cached=True,
        )


# ---------------------------------------------------------------- environment


def python_path(root: Path, paths: list[str]) -> str:
    return os.pathsep.join(str(root / p) for p in paths)


def gate_env(cfg: dict[str, Any], gate: dict[str, Any], root: Path, cache: Path, out_dir: Path) -> dict[str, str]:
    """The CI environment for one gate: the caller's environment plus worktree-local paths.

    Caches and coverage data are redirected to the run dir so gates leave the worktree clean.
    """
    env = dict(os.environ)
    env.update(gate.get("env", {}))
    repo_paths = python_path(root, cfg["repo"].get("python_paths", []))
    env["PYTHONPATH"] = os.pathsep.join(p for p in (repo_paths, str(PLUGIN_DIR)) if p)
    if repo_paths:
        env["MYPYPATH"] = repo_paths
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["MYPY_CACHE_DIR"] = str(cache / "mypy")
    env["RUFF_CACHE_DIR"] = str(cache / "ruff")
    env["COVERAGE_FILE"] = str(out_dir / ".coverage")
    if gate["parser"] == "pytest":
        env["PYTEST_ADDOPTS"] = " ".join(
            x for x in (env.get("PYTEST_ADDOPTS", ""), "-p adrev_pytest_plugin -p no:cacheprovider") if x
        )
        env["ADREV_PYTEST_REPORT"] = str(out_dir / "pytest-report.json")
    return env


def import_path_problems(cfg: dict[str, Any], root: Path) -> list[str]:
    """Code run in a worktree must import that worktree's code."""
    problems = []
    env = dict(os.environ)
    env["PYTHONPATH"] = python_path(root, cfg["repo"].get("python_paths", []))
    for check in cfg["repo"].get("import_checks", []):
        out = run(
            [sys.executable, "-c", f"import {check['module']} as m; print(m.__file__)"],
            cwd=root,
            env=env,
            check=False,
            replace_env=True,
        )
        location = out.stdout.strip()
        expected = (root / check["path_prefix"]).resolve()
        if out.returncode != 0 or not location or not Path(location).resolve().is_relative_to(expected):
            problems.append(f"{check['module']} imports from {location or out.stderr.strip()[-200:]}, not {expected}")
    return problems


def toolchain_fingerprint(cfg: dict[str, Any], repo: Path, base_tree: str) -> str:
    """Hash of the tools on PATH used by the gates plus the base's lockfiles."""
    parts = [sys.version]
    for gate in cfg["repo"]["gates"]:
        for argv in (gate["argv"], gate.get("diagnostics_argv") or []):
            if argv:
                exe = shutil.which(argv[0])
                parts.append(f"{argv[0]}={exe}:{os.stat(exe).st_mtime_ns if exe else 'missing'}")
    globs = cfg["repo"]["file_classes"].get("lockfiles", []) + cfg["repo"]["file_classes"].get(
        "dependency_manifests", []
    )
    listing = git(repo, "ls-tree", "-r", base_tree)
    for line in listing.splitlines():
        meta, _, path = line.partition("\t")
        if glob_match(path, globs):
            parts.append(f"{path}:{meta.split()[2]}")
    return sha256_text("\n".join(sorted(parts)))


def base_cache_key(gate: dict[str, Any], base_tree: str, toolchain: str, cfg: dict[str, Any]) -> str:
    return sha256_text(
        canonical_json(
            {
                "gate": gate,
                "tree": base_tree,
                "toolchain": toolchain,
                "python_paths": cfg["repo"].get("python_paths", []),
            }
        )
    )


# ---------------------------------------------------------------- review-input overlay


def differing_review_inputs(repo: Path, cfg: dict[str, Any], base: str, head: str) -> list[str]:
    from checks import tool_tables_differ

    changed = [p for p in git(repo, "diff", "--name-only", "--no-renames", base, head).split("\n") if p]
    hits = [p for p in changed if glob_match(p, cfg["repo"].get("review_inputs", []))]
    if "pyproject.toml" in changed and tool_tables_differ(repo, cfg, base, head, "pyproject.toml"):
        hits.append("pyproject.toml")
    return sorted(set(hits))


def overlay_base_inputs(repo: Path, base: str, head_root: Path, paths: list[str]) -> None:
    """Replace review-input files in the head worktree with their merge-base copies."""
    for rel in paths:
        target = head_root / rel
        exists = run(["git", "-C", str(repo), "cat-file", "-e", f"{base}:{rel}"], check=False).returncode == 0
        if exists:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(run_bytes_show(repo, base, rel))
        elif target.exists():
            target.unlink()


def run_bytes_show(repo: Path, rev: str, rel: str) -> bytes:
    from common import run_bytes

    return run_bytes(["git", "-C", str(repo), "show", f"{rev}:{rel}"])


# ---------------------------------------------------------------- spawning and collecting


def spawn(
    run_obj: Any,
    cfg: dict[str, Any],
    gate: dict[str, Any],
    side: str,
    root: Path,
    extra_argv: list[str] | None = None,
    label: str = "",
) -> str:
    name = f"{gate['name']}-{side}{label}"
    out_dir = run_obj.dir / "rounds" / f"r{run_obj.state['round']}" / "gates" / name
    out_dir.mkdir(parents=True, exist_ok=True)
    cache = run_obj.dir / "cache" / side
    env = gate_env(cfg, gate, root, cache, out_dir)
    argv = list(gate["argv"]) + list(extra_argv or [])
    spec = {
        "argv": argv,
        "diagnostics_argv": gate.get("diagnostics_argv") if not extra_argv else None,
        "cwd": str(root),
        "out_dir": str(out_dir),
        "collect_files": [gate["coverage_xml"]] if gate.get("coverage_xml") and not extra_argv else [],
    }
    spec_path = out_dir / "spec.json"
    spec_path.write_text(canonical_json(spec), encoding="utf-8")
    job = run_obj.spawn_job(
        "gate",
        [sys.executable, "-E", "-s", str(SCRIPTS_DIR / "gatejob.py"), str(spec_path)],
        root,
        env,
        int(gate["timeout_s"]),
        meta={"gate": gate["name"], "side": side, "out_dir": str(out_dir)},
    )
    return str(job)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def collect(gate: dict[str, Any], side: str, out_dir: Path, root: Path, job_status: str) -> SideResult:
    """Parse one gate run. A crash, timeout or unparseable output makes the result unusable."""
    result_file = out_dir / "result.json"
    if job_status != "done" or not result_file.is_file():
        return SideResult(gate["name"], side, "unusable", None, problem=f"job {job_status}")
    meta = read_json(result_file)
    code = meta.get("main_exit")
    main_out = _read(out_dir / "main.out")
    diag_out = _read(out_dir / "diag.out")
    parser = gate["parser"]
    res = SideResult(gate["name"], side, "ok", code)
    try:
        if code is None:
            raise ValueError("command could not start: " + _read(out_dir / "main.err")[-300:])
        if parser == "ruff":
            if code not in (0, 1):
                raise ValueError(f"ruff exited {code}")
            text = diag_out if gate.get("diagnostics_argv") else main_out
            res.diagnostics = parsers.parse_ruff_json(text, root)
        elif parser == "ruff-format":
            if code not in (0, 1):
                raise ValueError(f"ruff format exited {code}")
            res.diagnostics = parsers.parse_ruff_format(main_out + "\n" + _read(out_dir / "main.err"), root)
        elif parser == "mypy":
            if code not in (0, 1):
                raise ValueError(f"mypy gate exited {code}")
            if gate.get("diagnostics_argv"):
                if meta.get("diag_exit") not in (0, 1):
                    raise ValueError(f"mypy diagnostics exited {meta.get('diag_exit')}")
                res.diagnostics = parsers.parse_mypy_json(diag_out, root)
            else:
                res.diagnostics = parsers.parse_mypy_text(main_out, root)
            res.error_count = len(res.diagnostics)
        elif parser == "pytest":
            if code not in (0, 1):
                raise ValueError(f"pytest exited {code}")
            report_path = out_dir / "pytest-report.json"
            if not report_path.is_file():
                raise ValueError("pytest report missing (plugin not loaded?)")
            res.pytest = parsers.parse_pytest_report(report_path.read_text(encoding="utf-8"))
            res.diagnostics = parsers.pytest_failures(res.pytest)
            if gate.get("coverage_xml") and (out_dir / Path(gate["coverage_xml"]).name).is_file():
                res.coverage_xml = str(out_dir / Path(gate["coverage_xml"]).name)
        elif parser == "regex":
            res.diagnostics = parsers.parse_regex(diag_out or main_out, root, gate["regex"], gate["name"])
        elif parser != "exit":
            raise ValueError(f"unknown parser {parser}")
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as e:
        return SideResult(gate["name"], side, "unusable", code, problem=str(e)[:500])
    res.diagnostics = parsers.with_symbols(res.diagnostics, root)
    res.status = "ok" if code == 0 else "failed"
    return res


def save_base_cache(cache_dir: Path, key: str, result: SideResult) -> None:
    if result.status != "unusable":
        write_json(cache_dir / f"{key}.json", result.to_json())


def load_base_cache(cache_dir: Path, key: str) -> SideResult | None:
    path = cache_dir / f"{key}.json"
    if not path.is_file():
        return None
    data = read_json(path)
    if not isinstance(data, dict):
        return None
    return SideResult.from_json(data)


# ---------------------------------------------------------------- classification


def error_count_limit(root: Path, gate: dict[str, Any]) -> int | None:
    spec = gate.get("error_count_gate")
    if not spec:
        return None
    text = _read(root / spec["baseline_file"]).strip()
    try:
        return int(text)
    except ValueError:
        return None


def gate_findings(
    gate: dict[str, Any],
    base: SideResult,
    head: SideResult,
    diff: parsers.DiagnosticDiff,
    severity_map: dict[str, str],
    round_no: int,
    limit: int | None,
    exclude_nodes: set[str],
) -> list[dict[str, Any]]:
    """New diagnostics become findings: origin gate, confidence certain.

    Severity is major when the CI gate would fail at head (and the new diagnostics are why),
    otherwise severity_map's gate default (minor).
    """
    ci_fails = head.status == "failed" and (base.status != "failed" or bool(diff.new))
    if gate.get("error_count_gate") and limit is not None and head.error_count is not None:
        ci_fails = head.error_count > limit if gate["error_count_gate"]["op"] == "<=" else head.error_count >= limit
    severity = severity_map.get("gate:fails-ci", "major") if ci_fails else severity_map.get("gate:default", "minor")
    out = []
    for d in diff.new:
        node = dict(d.extra).get("node")
        if node and node in exclude_nodes:
            continue
        tool = d.tool
        category = CATEGORY_BY_TOOL.get(tool, "lint")
        anchor = f"{d.rule} {parsers.mask(d.message)}"
        count_note = ""
        if gate.get("error_count_gate") and limit is not None:
            count_note = f" The error count is {head.error_count} at head (limit {limit}, base {base.error_count})."
        out.append(
            fmod.make(
                origin="gate",
                category=category,
                severity=severity,
                title=f"{gate['name']}: new {tool} {d.rule} in {d.file}",
                file=d.file,
                start_line=d.line,
                symbol=d.symbol if d.symbol != "<module>" else None,
                anchor_text=anchor,
                conditions=f"Running the CI gate `{' '.join(gate['argv'])}` at head.",
                expected=f"No new {tool} diagnostics compared with the merge-base."
                + (" The CI gate passes." if ci_fails else ""),
                actual=f"{d.message}{count_note}",
                observed_output=f"{d.file}:{d.line}: [{d.rule}] {d.message}",
                tool_output_ref=f"rounds/r{round_no}/gates/{gate['name']}-head",
                round_no=round_no,
                raw_ref=f"gate:{gate['name']}",
            )
        )
    return out


def ci_failure_without_diagnostics(
    gate: dict[str, Any], base: SideResult, head: SideResult, diff: parsers.DiagnosticDiff, round_no: int
) -> dict[str, Any] | None:
    """A gate that newly fails at head even though no new diagnostic explains it."""
    if head.status != "failed" or base.status == "failed" or diff.new:
        return None
    ci_file = gate.get("ci_file") or "review.config.json"
    return fmod.make(
        origin="gate",
        category=CATEGORY_BY_TOOL.get(gate["parser"], "lint"),
        severity="major",
        title=f"CI gate {gate['name']} fails at head",
        file=ci_file,
        start_line=1,
        anchor_text=f"gate-fails {gate['name']}",
        kind="omission",
        conditions=f"Running `{' '.join(gate['argv'])}` at head.",
        expected="The gate passes, as it does at the merge-base.",
        actual=f"The gate exits {head.exit_code}.",
        tool_output_ref=f"rounds/r{round_no}/gates/{gate['name']}-head",
        round_no=round_no,
        raw_ref=f"gate:{gate['name']}",
    )


def classify_flaky(head_runs: list[str], base_runs: list[str]) -> str:
    """Rerun outcomes per side ('passed'/'failed'/...). Returns flaky | regression | intermittent."""
    if any(o in ("failed", "error") for o in base_runs):
        return "flaky"
    if all(o in ("failed", "error") for o in head_runs):
        return "regression"
    return "intermittent"


def ensure_clean(root: Path) -> str:
    dirty = git(root, "status", "--porcelain", "--untracked-files=all")
    return dirty


def reset_after_gates(root: Path, sha: str) -> None:
    from snapshot import reset_worktree

    reset_worktree(root, sha)


def raise_if_unusable(results: list[SideResult]) -> None:
    bad = [r for r in results if r.status == "unusable"]
    if bad:
        raise ReviewError("; ".join(f"{r.gate}@{r.side}: {r.problem}" for r in bad))
