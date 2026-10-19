"""Cross-round finding ledger: fingerprints, rounds seen, status history and reopen counts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import Any

REOPENING = {"open", "not-fixed", "regressed"}
CLOSED = {"fixed", "refuted", "refuted-by-adjudication", "suppressed", "acknowledged"}


def empty(run_id: str) -> dict[str, Any]:
    return {"run_id": run_id, "fingerprints": {}}


def record_round(ledger: dict[str, Any], round_no: int, findings: Iterable[Mapping[str, Any]]) -> None:
    """Record each finding's status for ``round_no`` and count reopenings.

    A fingerprint is reopened when it is open again after having been closed in an earlier round.
    """
    entries = ledger["fingerprints"]
    for finding in findings:
        fp = str(finding["fingerprint"])
        entry = entries.setdefault(
            fp,
            {
                "finding_ids": [],
                "first_round": round_no,
                "rounds_seen": [],
                "reopen_count": 0,
                "status_history": [],
                "anchor_text_sha": finding.get("anchor_text_sha"),
            },
        )
        if finding["id"] not in entry["finding_ids"]:
            entry["finding_ids"].append(finding["id"])
        if round_no not in entry["rounds_seen"]:
            entry["rounds_seen"].append(round_no)
        status = str(finding.get("status", "open"))
        history = entry["status_history"]
        previous = history[-1]["status"] if history else None
        if previous in CLOSED and status in REOPENING:
            entry["reopen_count"] += 1
        if not history or history[-1] != {"round": round_no, "status": status}:
            if history and history[-1]["round"] == round_no:
                history[-1] = {"round": round_no, "status": status}
            else:
                history.append({"round": round_no, "status": status})


def reopened_twice(ledger: Mapping[str, Any]) -> list[str]:
    return sorted(fp for fp, e in ledger["fingerprints"].items() if e["reopen_count"] >= 2)


def last_status(ledger: Mapping[str, Any], fingerprint: str) -> str | None:
    entry = ledger["fingerprints"].get(fingerprint)
    if not entry or not entry["status_history"]:
        return None
    return str(entry["status_history"][-1]["status"])
