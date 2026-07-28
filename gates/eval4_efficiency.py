"""Eval 4 — Efficiency (pure extraction from spans).

Tokens, wall-clock, turns, and estimated cost per run, aggregated per arm. Used
as the tie-breaker when both variants pass functional gating, and as the
threshold `max_tokens_per_case_p50` in the CI gate.

`est_cost_usd` is normally computed by the tracing layer from token counts; if a
run lacks it, we fall back to a coarse per-token estimate so the report is never
blank. Prices are USD per token (input, output) and easy to update.
"""

from __future__ import annotations

import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RunResult  # noqa: E402

# coarse fallback prices (USD/token); harness model = Sonnet.
PRICES = {
    "default": (3.0e-6, 15.0e-6),   # $3 / $15 per MTok (Sonnet-class)
}


def _est_cost(run: RunResult) -> float:
    if run.est_cost_usd:
        return run.est_cost_usd
    pin, pout = PRICES["default"]
    return run.tokens_input * pin + run.tokens_output * pout


def per_run(runs: list[RunResult]) -> list[dict]:
    return [{
        "case_id": r.case_id, "arm": r.arm, "trial": r.trial,
        "tokens_input": r.tokens_input, "tokens_output": r.tokens_output,
        "tokens_total": r.tokens_total, "turns": r.turns,
        "wall_clock_s": r.wall_clock_s, "est_cost_usd": round(_est_cost(r), 6),
    } for r in runs]


def metrics_by_arm(runs: list[RunResult]) -> dict:
    by_arm: dict[str, list[RunResult]] = {}
    for r in runs:
        by_arm.setdefault(r.arm, []).append(r)

    out: dict[str, dict] = {}
    for arm, rs in by_arm.items():
        toks = [r.tokens_total for r in rs]
        out[arm] = {
            "n": len(rs),
            "tokens_p50": statistics.median(toks) if toks else 0,
            "tokens_mean": statistics.mean(toks) if toks else 0,
            "turns_mean": statistics.mean([r.turns for r in rs]) if rs else 0,
            "wall_clock_mean": statistics.mean([r.wall_clock_s for r in rs]) if rs else 0,
            "cost_mean": statistics.mean([_est_cost(r) for r in rs]) if rs else 0,
        }
    return out
