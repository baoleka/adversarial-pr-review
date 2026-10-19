"""The verdict, decided in order:

1. any ESCALATE condition → ESCALATE;
2. anything blocks, a must-address finding is open, or a fix isn't FIXED → REQUEST_CHANGES under
   --no-fix, or another round with the author in loop mode;
3. otherwise APPROVE if every APPROVE condition holds, else ESCALATE naming the failed condition.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

OPEN_STATUSES = ("open", "not-fixed", "regressed")


@dataclass
class Decision:
    verdict: str  # APPROVE | REQUEST_CHANGES | ESCALATE | AUTHOR
    reason: str
    escalations: list[dict[str, Any]] = field(default_factory=list)


def open_ids(found: Sequence[Mapping[str, Any]], bucket: str) -> list[str]:
    return [f["id"] for f in found if f["bucket"] == bucket and f["status"] in OPEN_STATUSES]


def approve_failures(
    found: Sequence[Mapping[str, Any]],
    coverage_ratio: float,
    lanes_ok: bool,
    protected_dropped: Sequence[str],
    fixes_pending: Sequence[str],
) -> list[tuple[str, str]]:
    failures = []
    if open_ids(found, "blocking"):
        failures.append(("nothing-blocks", "blocking findings are open"))
    if open_ids(found, "must-address"):
        failures.append(("must-address-closed", "must-address findings are open"))
    if fixes_pending:
        failures.append(("fixes-verified", "fixes not verified as FIXED: " + ", ".join(fixes_pending)))
    contested = [f["id"] for f in found if f["bucket"] == "contested" and f["severity"] == "critical"]
    if contested:
        failures.append(("no-contested-critical", "contested critical findings: " + ", ".join(contested)))
    if not lanes_ok:
        failures.append(("lanes-succeeded", "too few review lanes succeeded"))
    if coverage_ratio < 1.0:
        failures.append(("coverage", f"review coverage is {coverage_ratio:.0%}"))
    if protected_dropped:
        failures.append(("protected-lanes", "required lanes dropped for budget: " + ", ".join(protected_dropped)))
    return failures


def decide(
    mode: str,
    escalations: Sequence[Mapping[str, Any]],
    found: Sequence[Mapping[str, Any]],
    coverage_ratio: float,
    lanes_ok: bool,
    protected_dropped: Sequence[str],
    fixes_pending: Sequence[str] = (),
) -> Decision:
    if escalations:
        conditions = sorted({str(e["condition"]) for e in escalations})
        return Decision("ESCALATE", "escalation: " + ", ".join(conditions), [dict(e) for e in escalations])
    blocking = open_ids(found, "blocking")
    must = open_ids(found, "must-address")
    if blocking or must or fixes_pending:
        parts = []
        if blocking:
            parts.append(f"{len(blocking)} blocking")
        if must:
            parts.append(f"{len(must)} must-address")
        if fixes_pending:
            parts.append(f"{len(fixes_pending)} fix(es) not verified")
        reason = ", ".join(parts)
        return Decision("REQUEST_CHANGES" if mode == "no-fix" else "AUTHOR", reason)
    failures = approve_failures(found, coverage_ratio, lanes_ok, protected_dropped, fixes_pending)
    if failures:
        escalated = [
            {"condition": f"approve-condition:{name}", "detail": detail, "finding_ids": [], "decision_needed": ""}
            for name, detail in failures
        ]
        return Decision("ESCALATE", "an APPROVE condition failed: " + "; ".join(d for _, d in failures), escalated)
    return Decision("APPROVE", "nothing blocks, every check passed and every selected lane succeeded")
