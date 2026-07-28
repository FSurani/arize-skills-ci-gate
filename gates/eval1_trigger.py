"""Eval 1 — Trigger correctness (deterministic, trace-based).

For each run, decide whether the skill's SKILL.md was loaded (`detect_triggered`,
defined once in common.py and shared with the harness + unit tests). Compare to
the case's `should_trigger` label:

  - false positive  = a hard-negative case that triggered   → FP rate over negatives
  - false negative  = a positive case that did NOT trigger   → FN rate over positives

Computed PER ARM, because the headline demo result is that variant_a's broad
description trips hard negatives (high FP) while variant_b's tight description
does not (demo beat 3).
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvaluationResult, RunResult, detect_triggered  # noqa: E402


def per_run(cases: list[dict], runs: list[RunResult], skill_name: str) -> list[dict]:
    """One trigger record per run — the per-run eval logged to AX."""
    should = {c["id"]: bool(c["should_trigger"]) for c in cases}
    out = []
    for r in runs:
        if r.arm == "skill_off":
            continue  # baseline never has the skill installed
        triggered = detect_triggered(r.tool_spans, skill_name)
        exp = should.get(r.case_id)
        out.append({
            "case_id": r.case_id, "arm": r.arm, "trial": r.trial,
            "should_trigger": exp, "triggered": triggered,
            "correct": (exp == triggered) if exp is not None else None,
        })
    return out


def metrics_by_arm(cases: list[dict], runs: list[RunResult], skill_name: str) -> dict:
    """FP/FN rates per arm. A case counts as triggered if ANY of its trials did
    (a load is a load); negatives typically run 1 trial so this is moot there."""
    should = {c["id"]: bool(c["should_trigger"]) for c in cases}
    # collapse trials → per (arm, case) triggered-if-any
    by_arm: dict[str, dict[str, bool]] = {}
    for r in runs:
        if r.arm == "skill_off":
            continue
        d = by_arm.setdefault(r.arm, {})
        d[r.case_id] = d.get(r.case_id, False) or detect_triggered(r.tool_spans, skill_name)

    result: dict[str, dict] = {}
    for arm, triggered_map in by_arm.items():
        pos = [cid for cid in triggered_map if should.get(cid) is True]
        neg = [cid for cid in triggered_map if should.get(cid) is False]
        fps = [cid for cid in neg if triggered_map[cid]]        # negative that fired
        fns = [cid for cid in pos if not triggered_map[cid]]    # positive that didn't
        result[arm] = {
            "n_pos": len(pos), "n_neg": len(neg),
            "fp_rate": (len(fps) / len(neg)) if neg else 0.0,
            "fn_rate": (len(fns) / len(pos)) if pos else 0.0,
            "false_positives": sorted(fps),
            "false_negatives": sorted(fns),
            "triggered": triggered_map,
        }
    return result


def to_eval_result(arm: str, m: dict) -> EvaluationResult:
    ok = m["fp_rate"] == 0 and m["fn_rate"] == 0
    return EvaluationResult(
        label="clean" if ok else "errors",
        score=1.0 - max(m["fp_rate"], m["fn_rate"]),
        explanation=(f"arm={arm}: FP={m['fp_rate']:.2f} ({m['false_positives']}), "
                     f"FN={m['fn_rate']:.2f} ({m['false_negatives']})"),
        metadata={"gate": "eval1_trigger", "arm": arm, **{k: v for k, v in m.items() if k != "triggered"}},
    )
