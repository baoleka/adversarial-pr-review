"""Cost model, per-call estimates, the budget gate, and usage from `claude -p` result JSON.

Estimates follow the plan's cost model: a fixed Claude Code context per call, a brief, a number
of turns each adding new context, and output per turn that depends on effort. Measured
`total_cost_usd` from each call replaces the estimate in the run's accounting.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

FIXED_CONTEXT_TOKENS = 20_000
NEW_TOKENS_PER_TURN = 2_500
OUTPUT_PER_TURN = {"low": 300, "medium": 700, "high": 1_000, "xhigh": 1_400, "max": 2_000}
TURNS = {
    "breaker": 20,
    "external": 14,
    "contracts": 14,
    "tests": 10,
    "security": 14,
    "interface": 12,
    "delta": 14,
    "verifier": 10,
    "fixcheck": 8,
    "adjudicator": 10,
    "probe": 2,
}


def price(cfg: Mapping[str, Any], model: str) -> Mapping[str, float]:
    models = cfg["pricing"]["models"]
    if model in models:
        return dict(models[model])
    # Unknown model or alias: assume the most expensive configured price (never underestimate).
    return max((dict(p) for p in models.values()), key=lambda p: p["output"])


def estimate_call(cfg: Mapping[str, Any], lane_kind: str, model: str, effort: str, brief_tokens: int) -> float:
    p = price(cfg, model)
    turns = TURNS.get(lane_kind, 12)
    first = FIXED_CONTEXT_TOKENS + brief_tokens
    avg_context = first + turns * NEW_TOKENS_PER_TURN / 2
    cost = first * p["cache_write"]
    cost += turns * avg_context * p["cache_read"]
    cost += turns * NEW_TOKENS_PER_TURN * p["cache_write"]
    cost += turns * OUTPUT_PER_TURN.get(effort, 1_400) * p["output"]
    # Calibration (limits.estimate_scale): on the dev runs of 2026-10-18, measured cost was 0.24-0.53
    # of this model's estimate (median per call type) and at most 1.05; 0.6 keeps every observed call
    # within its 3x per-call cap while letting large changes fit the budget.
    scale = float(cfg.get("limits", {}).get("estimate_scale", 0.6))
    return round(scale * cost / 1_000_000, 4)


def tokens_of(text: str) -> int:
    """Rough token count (about 4 characters per token) for budgeting only."""
    return max(1, len(text) // 4)


def budget_limit(cfg: Mapping[str, Any], mode: str, override: float | None) -> float:
    if override is not None:
        return float(override)
    key = "per_run_loop_usd" if mode == "loop" else "per_run_usd"
    return float(cfg["limits"]["budget"][key])


def spent(state: Mapping[str, Any]) -> float:
    return round(sum(float(c.get("cost_usd") or 0.0) for c in state.get("calls", [])), 4)


def can_afford(state: Mapping[str, Any], cfg: Mapping[str, Any], estimate: float) -> bool:
    limit = budget_limit(cfg, state["mode"], state["args"].get("budget_usd"))
    return spent(state) + estimate <= limit


def remaining(state: Mapping[str, Any], cfg: Mapping[str, Any]) -> float:
    limit = budget_limit(cfg, state["mode"], state["args"].get("budget_usd"))
    return round(max(0.0, limit - spent(state)), 4)


def usage_from_result(result: Mapping[str, Any]) -> dict[str, Any]:
    usage = result.get("usage") or {}
    return {
        "cost_usd": float(result.get("total_cost_usd") or 0.0),
        "input": int(usage.get("input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write": int(usage.get("cache_creation_input_tokens") or 0),
        "turns": int(result.get("num_turns") or 0),
        "model_usage": result.get("modelUsage") or {},
    }
