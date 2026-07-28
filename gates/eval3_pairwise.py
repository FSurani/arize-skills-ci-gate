"""Eval 3 — Pairwise blinded judge (Skill A variants only).

For each task case, present variant_a's and variant_b's final outputs to a judge
that is blind to variant identity, in a per-case randomized A/B order, and record
winner + rationale plus an absolute 1-5 score per run.

Blinding is load-bearing and UNIT-TESTED: the text handed to the
judge must contain NO variant-identifying strings. `blinded_inputs` is returned so
the test can grep it. Order is derived deterministically from the case id (no
RNG) so runs are reproducible.

Second method (AX differentiator, demo beat 3): the SAME experiment runs
are ALSO scored by AX's built-in Agent-as-a-Judge, registered/triggered in the
experiment layer (experiments/run_experiment.py + ci/gate.py via `ax evaluators`
/ `ax tasks trigger-run`). Both methods are shown side-by-side in the report.
"""

from __future__ import annotations

import hashlib
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import RunResult  # noqa: E402
from judge import pairwise_judge  # noqa: E402

# strings that would leak variant identity — scrubbed before judging
_LEAK_PATTERNS = [
    re.compile(r"variant[_\s-]?[ab]", re.I),
    re.compile(r"\.claude/skills/[^\s)\"']+", re.I),
    re.compile(r"\b(arm|version)\s*[:=]\s*variant[_\s-]?[ab]\b", re.I),
]


def sanitize(text: str) -> str:
    """Remove any variant-identifying markers from an output before judging."""
    out = text or ""
    for pat in _LEAK_PATTERNS:
        out = pat.sub("[redacted]", out)
    return out


def _order_for(case_id: str) -> bool:
    """Deterministic A/B swap decision (True = swap) from the case id — no RNG."""
    h = int(hashlib.sha1(case_id.encode()).hexdigest(), 16)
    return bool(h & 1)


def judge_case(case: dict, run_a: RunResult, run_b: RunResult, classify_fn=None) -> dict:
    """Blind-compare variant_a (run_a) vs variant_b (run_b) for one case."""
    task = case["prompt"]
    out_a = sanitize(run_a.final_output)
    out_b = sanitize(run_b.final_output)

    swap = _order_for(case["id"])
    # what the judge sees as "A"/"B"
    first, second = (out_b, out_a) if swap else (out_a, out_b)
    first_is = "variant_b" if swap else "variant_a"
    second_is = "variant_a" if swap else "variant_b"

    verdict = pairwise_judge(task, first, second, classify_fn=classify_fn)

    # map A/B winner back to variant identity
    if verdict["winner"] == "A":
        winner_variant = first_is
    elif verdict["winner"] == "B":
        winner_variant = second_is
    else:
        winner_variant = "tie"

    score_first, score_second = verdict["score_a"], verdict["score_b"]
    score_a = score_second if swap else score_first        # variant_a's absolute score
    score_b = score_first if swap else score_second        # variant_b's absolute score

    return {
        "case_id": case["id"],
        "winner_variant": winner_variant,
        "score_variant_a": score_a,
        "score_variant_b": score_b,
        "rationale": verdict["rationale"],
        # exposed for the blinding unit test — must be free of variant markers:
        "blinded_inputs": f"{task}\n\n{first}\n\n{second}",
        "order_swapped": swap,
    }


def aggregate(results: list[dict]) -> dict:
    n = len(results)
    if not n:
        return {"n": 0}
    wins_a = sum(1 for r in results if r["winner_variant"] == "variant_a")
    wins_b = sum(1 for r in results if r["winner_variant"] == "variant_b")
    ties = sum(1 for r in results if r["winner_variant"] == "tie")
    return {
        "n": n,
        "variant_a_win_rate": wins_a / n,
        "variant_b_win_rate": wins_b / n,
        "tie_rate": ties / n,
        "variant_a_mean_score": sum(r["score_variant_a"] for r in results) / n,
        "variant_b_mean_score": sum(r["score_variant_b"] for r in results) / n,
    }
