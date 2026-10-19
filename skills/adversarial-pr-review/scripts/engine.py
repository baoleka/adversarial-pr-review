"""Phase engine primitives shared by the driver and the phase modules."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from common import read_json
from state import Run

ROUND_ONE = [
    "SNAPSHOT",
    "TRIAGE",
    "GATES",
    "CHECKS",
    "BRIEFS",
    "PROBE",
    "LANES",
    "NORMALIZE",
    "REPLAY",
    "VERIFY",
    "SYNTHESIZE",
    "VERDICT",
]
LATER_ROUNDS = [
    "SNAPSHOT",
    "TRIAGE",
    "GATES",
    "CHECKS",
    "PROBE",
    "FIXREPLAY",
    "FIXCHECK",
    "ADJUDICATE",
    "BRIEFS",
    "LANES",
    "NORMALIZE",
    "REPLAY",
    "VERIFY",
    "SYNTHESIZE",
    "VERDICT",
]


# ---------------------------------------------------------------- steps


@dataclass
class Step:
    """What a phase handler wants next."""

    kind: str  # advance | wait | author | ask | terminal
    jobs: list[str] = field(default_factory=list)
    payload: dict[str, Any] = field(default_factory=dict)

    @staticmethod
    def advance() -> Step:
        return Step("advance")

    @staticmethod
    def wait(jobs: list[str], any_done: bool = False) -> Step:
        """Wait for all ``jobs`` (or, with ``any_done``, for the first one to finish)."""
        return Step("wait", jobs=jobs, payload={"any_done": any_done})

    @staticmethod
    def author(payload: dict[str, Any]) -> Step:
        return Step("author", payload=payload)

    @staticmethod
    def ask(question: str, key: str) -> Step:
        return Step("ask", payload={"question": question, "key": key})

    @staticmethod
    def terminal(verdict: str) -> Step:
        return Step("terminal", payload={"verdict": verdict})


@dataclass
class Ctx:
    run: Run
    repo: Path

    @property
    def state(self) -> dict[str, Any]:
        return self.run.state

    @property
    def round(self) -> int:
        return int(self.state["round"])

    def config(self) -> dict[str, Any]:
        data = read_json(self.run.dir / "config.json")
        assert isinstance(data, dict)
        return data

    def phase_state(self) -> dict[str, Any]:
        key = f"r{self.round}:{self.state['phase']}"
        store: dict[str, dict[str, Any]] = self.state.setdefault("phase_state", {})
        return store.setdefault(key, {})

    def escalate(self, condition: str, detail: str, finding_ids: list[str] | None = None, decision: str = "") -> None:
        entry = {
            "condition": condition,
            "detail": detail,
            "finding_ids": finding_ids or [],
            "decision_needed": decision,
            "round": self.round,
        }
        if entry not in self.state.setdefault("escalations", []):
            self.state["escalations"].append(entry)
        self.run.event("escalate", condition=condition, detail=detail)


PhaseFn = Callable[[Ctx], Step]
PHASES: dict[str, PhaseFn] = {}


def phase(name: str) -> Callable[[PhaseFn], PhaseFn]:
    def register(fn: PhaseFn) -> PhaseFn:
        PHASES[name] = fn
        return fn

    return register


def phase_order(round_no: int) -> list[str]:
    return ROUND_ONE if round_no == 1 else LATER_ROUNDS


def next_phase(ctx: Ctx) -> str:
    order = phase_order(ctx.round)
    current = ctx.state["phase"]
    if current == "INIT":
        return order[0]
    index = order.index(current)
    return order[index + 1] if index + 1 < len(order) else "VERDICT"
