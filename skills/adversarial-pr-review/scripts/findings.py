"""Finding construction, fingerprints and IDs shared by gates, checks and lanes."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from common import SCHEMAS_DIR, read_json
from jsonschema_lite import validate

_WS = re.compile(r"\s+")
LANE_CATEGORIES = (
    "correctness",
    "regression",
    "behavior-change",
    "contract",
    "error-handling",
    "concurrency",
    "resource",
    "security",
    "test-quality",
    "scope",
    "performance",
)
SEVERITY_RANK = {"critical": 0, "major": 1, "minor": 2, "nit": 3}
_SCHEMA: dict[str, Any] | None = None


def schema() -> dict[str, Any]:
    global _SCHEMA
    if _SCHEMA is None:
        data = read_json(SCHEMAS_DIR / "finding.schema.json")
        assert isinstance(data, dict)
        _SCHEMA = data
    return _SCHEMA


def normalize_anchor(text: str) -> str:
    return _WS.sub(" ", text).strip()


def fingerprint(file: str, symbol: str | None, side: str, anchor_text: str) -> str:
    raw = "\x1f".join([file, symbol or "<module>", side, normalize_anchor(anchor_text)])
    return hashlib.sha1(raw.encode("utf-8"), usedforsecurity=False).hexdigest()


def finding_id(fp: str) -> str:
    return f"F-{fp[:12]}"


def clamp(text: str, limit: int) -> str:
    text = text if text else "(none)"
    return text if len(text) <= limit else text[: limit - 1] + "…"


def make(
    *,
    origin: str,
    category: str,
    severity: str,
    title: str,
    file: str,
    start_line: int,
    end_line: int | None = None,
    side: str = "head",
    quote: str | None = None,
    symbol: str | None = None,
    anchor_text: str | None = None,
    introduced_by: list[dict[str, Any]] | None = None,
    conditions: str,
    expected: str,
    actual: str,
    evidence_level: str = "executed",
    observed_output: str | None = None,
    tool_output_ref: str | None = None,
    suggested_fix: str | None = None,
    kind: str = "defect",
    round_no: int = 1,
    raw_ref: str = "",
    lane: str | None = None,
    model: str | None = None,
    lens: str | None = None,
    citation_chain: list[dict[str, Any]] | None = None,
    repro: dict[str, Any] | None = None,
    self_confidence: float | None = None,
) -> dict[str, Any]:
    """Build a complete finding with pipeline defaults; gate and check findings are certain."""
    start_line = max(1, int(start_line))
    end_line = max(start_line, int(end_line or start_line))
    fp = fingerprint(file, symbol, side, anchor_text if anchor_text is not None else (quote or title))
    certain = origin in ("gate", "check")
    return {
        "id": finding_id(fp),
        "fingerprint": fp,
        "origin": origin,
        "category": category,
        "severity": severity,
        "title": clamp(title, 160) if len(title) >= 5 else (title + " " * 5)[:5],
        "kind": kind,
        "location": {
            "side": side,
            "file": file,
            "start_line": start_line,
            "end_line": end_line,
            "quote": clamp(quote, 2000) if quote else None,
            "symbol": symbol,
        },
        "introduced_by": introduced_by
        if introduced_by is not None
        else [{"side": side, "file": file, "line": start_line}],
        "failure_scenario": {
            "conditions": clamp(conditions, 2000),
            "expected": clamp(expected, 2000),
            "actual": clamp(actual, 2000),
        },
        "evidence": {
            "level": evidence_level,
            "citation_chain": citation_chain or [],
            "repro": repro,
            "observed_output": clamp(observed_output, 8000) if observed_output else None,
            "tool_output_ref": tool_output_ref,
        },
        "suggested_fix": clamp(suggested_fix, 4000) if suggested_fix else None,
        "self_confidence": self_confidence,
        "lane": lane,
        "model": model,
        "lens": lens,
        "provenance": [
            {
                "origin": origin,
                "lane": lane,
                "model": model,
                "lens": lens,
                "round": round_no,
                "raw_ref": raw_ref or origin,
            }
        ],
        "replay": None,
        "verification": None,
        "preexisting": {"value": False, "reason": "none"},
        "confidence": "certain" if certain else "unverified",
        "blocking": False,
        "bucket": "non-blocking",
        "status": "open",
        "rounds_seen": [round_no],
        "disposition": None,
        "adjudication": None,
    }


def problems(finding: Mapping[str, Any]) -> list[str]:
    return validate(finding, schema())


def sort_key(finding: Mapping[str, Any]) -> tuple[int, str, int, str]:
    loc = finding["location"]
    return (SEVERITY_RANK[finding["severity"]], loc["file"], int(loc["start_line"]), finding["id"])
