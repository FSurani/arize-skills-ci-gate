#!/usr/bin/env python3
"""ci/gate.py — gate a skill in CI, on Arize. One linear flow:

    1. Eval 0   — structural + SECURITY scan (local, runs first, bounces bad skills)
    2. harness  — run the skill over its cases (traced to Arize); --mock for no tokens
    3. Arize    — push an experiment; the Eval Hub evaluators score it server-side
    4. read     — read the scores back from Arize
    5. call     — compare to ci/thresholds.yaml, exit 0 (pass) / 1 (fail)

Provision the evaluators + datasets once with `python experiments/setup.py`.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "gates", REPO / "harness", REPO / "experiments"):
    sys.path.insert(0, str(p))

from common import load_cases                        # noqa: E402
import eval0_structural as eval0                      # noqa: E402
from run_case import run_matrix_async, DEFAULT_WORK_ROOT  # noqa: E402
import eval_hub as hub                                # noqa: E402

SKILLS = REPO / "skills"
DEFAULT_ARM = {"api-helper": "variant_b", "story-writer": "v1"}


def skill_dir(skill: str, arm: str) -> Path:
    """Directory holding the SKILL.md for Eval 0 (api-helper has variant subdirs)."""
    base = SKILLS / skill
    return base / arm if (base / arm).is_dir() else base


def load_thresholds(skill: str) -> dict:
    cfg = yaml.safe_load((REPO / "ci" / "thresholds.yaml").read_text())
    thr = dict(cfg.get("default", {}))
    thr.update((cfg.get("skills", {}) or {}).get(skill, {}) or {})
    return thr


def gate(skill: str, arm: str, *, mock: bool, max_cases: int | None,
         dry_run: bool, work_root: Path, concurrency: int) -> int:
    thr = load_thresholds(skill)
    print(f"\n=== GATE: {skill} ({arm}) ===")

    # 1. Eval 0 — structural + security (short-circuit)
    res = eval0.evaluate_skill(skill_dir(skill, arm))
    print(f"[eval0] {'PASS' if res.passed else 'FAIL'}  {'' if res.passed else res.explanation[:200]}")
    if not res.passed:
        print("\nVERDICT: FAIL — bounced at the door (Eval 0).")
        return 1
    if dry_run:
        print("[dry-run] Eval 0 passed; would run the harness + Arize experiment. Exiting 0.")
        return 0

    # 2. harness — run the skill over its cases (traced to Arize)
    cases = load_cases(REPO / "datasets" / f"{skill.replace('-', '_')}_cases.jsonl")
    if max_cases:
        pos = [c for c in cases if c.get("check")][:max_cases]
        neg = [c for c in cases if not c.get("check")][:max(1, max_cases // 3)]
        cases = pos + neg
    triples = [(c, arm, 0) for c in cases]
    print(f"[harness] {len(triples)} runs ({'mock' if mock else 'real'})...")
    runs = asyncio.run(run_matrix_async(triples, skill, work_root, mock=mock, concurrency=concurrency))

    # 3-4. Arize experiment scored by the Eval Hub → read the scores back
    ds_id, exp_id = hub.run_experiment(skill, arm, runs)
    m = hub.read_metrics(skill, ds_id, exp_id)

    # 5. threshold → verdict
    checks = {
        "trigger_fp": m["fp_rate"] <= thr["trigger_fp_rate_max"],
        "trigger_fn": m["fn_rate"] <= thr["trigger_fn_rate_max"],
        "functional": m["functional_pass_rate"] >= thr["functional_pass_rate_min"],
        "efficiency": m["tokens_p50"] <= thr["max_tokens_per_case_p50"],
    }
    passed = all(checks.values())

    print(f"\n{'metric':<24}{'value':>10}  threshold")
    print(f"{'trigger FP rate':<24}{m['fp_rate']:>10.2f}  <= {thr['trigger_fp_rate_max']}")
    print(f"{'trigger FN rate':<24}{m['fn_rate']:>10.2f}  <= {thr['trigger_fn_rate_max']}")
    print(f"{'functional pass@1':<24}{m['functional_pass_rate']:>10.2f}  >= {thr['functional_pass_rate_min']}")
    print(f"{'tokens p50':<24}{int(m['tokens_p50']):>10d}  <= {thr['max_tokens_per_case_p50']}")
    print(f"\n[arize] scores read from experiment {exp_id}")
    print(f"VERDICT: {'PASS' if passed else 'FAIL'}  ({skill}/{arm}; source: Arize Eval Hub)")
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description="Gate a skill in CI, on Arize.")
    ap.add_argument("--skill", required=True, choices=["api-helper", "story-writer"])
    ap.add_argument("--arm", default=None, help="which variant to gate (default: the candidate)")
    ap.add_argument("--mock", action="store_true", help="offline simulator (no tokens)")
    ap.add_argument("--max-cases", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="Eval 0 only, then stop")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    args = ap.parse_args()

    arm = args.arm or DEFAULT_ARM[args.skill]
    sys.exit(gate(args.skill, arm, mock=args.mock, max_cases=args.max_cases,
                  dry_run=args.dry_run, work_root=Path(args.work_root), concurrency=args.concurrency))


if __name__ == "__main__":
    main()
