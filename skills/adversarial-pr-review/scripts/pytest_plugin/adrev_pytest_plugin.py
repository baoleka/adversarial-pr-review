"""pytest plugin loaded with `-p adrev_pytest_plugin`: records collected test IDs and outcomes.

The report goes to the file named by ADREV_PYTEST_REPORT. It writes test IDs at
pytest_collection_finish (so `-q` output and addopts never matter) and one outcome per test.
"""

from __future__ import annotations

import json
import os
from typing import Any

_REPORT: dict[str, Any] = {"collected": [], "outcomes": {}, "exitstatus": None}


def _write() -> None:
    path = os.environ.get("ADREV_PYTEST_REPORT")
    if path:
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(_REPORT, fh, sort_keys=True)
        os.replace(tmp, path)


def pytest_collection_finish(session: Any) -> None:
    _REPORT["collected"] = [item.nodeid for item in session.items]
    _write()


def pytest_runtest_logreport(report: Any) -> None:
    entry = _REPORT["outcomes"].setdefault(
        report.nodeid,
        {"outcome": "passed", "file": report.location[0], "line": (report.location[1] or 0) + 1, "message": ""},
    )
    if hasattr(report, "wasxfail"):
        entry["outcome"] = "xpassed" if report.passed else "xfailed"
        return
    if report.failed:
        entry["outcome"] = "error" if report.when in ("setup", "teardown") else "failed"
        text = str(getattr(report, "longreprtext", "") or "")
        entry["message"] = (text.strip().splitlines() or [""])[-1][:2000]
    elif report.skipped and entry["outcome"] == "passed":
        entry["outcome"] = "skipped"
        if isinstance(report.longrepr, tuple) and len(report.longrepr) == 3:
            entry["message"] = str(report.longrepr[2])[:2000]


def pytest_sessionfinish(session: Any, exitstatus: Any) -> None:
    _REPORT["exitstatus"] = int(exitstatus)
    _write()
