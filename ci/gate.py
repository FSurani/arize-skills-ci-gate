#!/usr/bin/env python3
"""ci/gate.py — gate one skill in CI, on Arize.

Read it top-to-bottom. Each step is tagged so it's clear what runs where:

  LOCAL  = a helper in this repo
  ARIZE  = talks to Arize (via the Eval Hub, wrapped in experiments/eval_hub.py)

  1. LOCAL  Eval 0 — structural + security scan        (runs first; bounces bad skills)
  2. LOCAL  harness — run the skill on its cases         (traced to Arize)
  3. ARIZE  push an experiment; the Eval Hub scores it   (server-side evaluators)
  4. ARIZE  read the scores back
  5. LOCAL  compare to ci/thresholds.yaml → exit 0/1

Provision the Arize evaluators + datasets once: `python experiments/setup.py`.
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

from common import load_cases                        # LOCAL: dataset loader
import eval0_structural as eval0                      # LOCAL: Eval 0 (structural + security)
from run_case import run_matrix_async, DEFAULT_WORK_ROOT  # LOCAL: the sandboxed skill harness
import eval_hub                                       # ARIZE: Eval Hub (evaluators + experiments) via the ax CLI

SKILLS = REPO / "skills"
# the candidate variant to gate when --variant isn't given
DEFAULT_VARIANT = {"api-helper": "variant_b", "story-writer": "v1"}


def skill_md_dir(skill: str, variant: str) -> Path:
    """Directory holding the SKILL.md for Eval 0 (api-helper has variant subdirs)."""
    base = SKILLS / skill
    return base / variant if (base / variant).is_dir() else base


def load_thresholds(skill: str) -> dict:
    cfg = yaml.safe_load((REPO / "ci" / "thresholds.yaml").read_text())
    thr = dict(cfg.get("default", {}))
    thr.update((cfg.get("skills", {}) or {}).get(skill, {}) or {})
    return thr


def gate(skill: str, variant: str, *, mock: bool, max_cases: int | None,
         dry_run: bool, work_root: Path, concurrency: int) -> int:
    thresholds = load_thresholds(skill)
    print(f"\n=== GATE: {skill} ({variant}) ===")

    # 1. LOCAL — Eval 0: structural + security scan (short-circuit on failure)
    security = eval0.evaluate_skill(skill_md_dir(skill, variant))
    print(f"[eval0] {'PASS' if security.passed else 'FAIL'}  "
          f"{'' if security.passed else security.explanation[:200]}")
    if not security.passed:
        print("\nVERDICT: FAIL — bounced at the door (Eval 0).")
        return 1
    if dry_run:
        print("[dry-run] Eval 0 passed; would run the harness + Arize experiment. Exiting 0.")
        return 0

    # 2. LOCAL — run the skill over its cases with the harness (traced to Arize)
    cases = load_cases(REPO / "datasets" / f"{skill.replace('-', '_')}_cases.jsonl")
    if max_cases:
        positives = [c for c in cases if c.get("check")][:max_cases]
        negatives = [c for c in cases if not c.get("check")][:max(1, max_cases // 3)]
        cases = positives + negatives
    print(f"[harness] {len(cases)} cases ({'mock' if mock else 'real'})...")
    runs = asyncio.run(run_matrix_async(
        [(c, variant, 0) for c in cases], skill, work_root, mock=mock, concurrency=concurrency))

    # 3 + 4. ARIZE — create an experiment, let the Eval Hub score it, read the scores back
    dataset_id, experiment_id = eval_hub.run_experiment(skill, variant, runs)
    scores = eval_hub.read_metrics(skill, dataset_id, experiment_id)

    # 5. LOCAL — compare the Arize scores to the thresholds → verdict
    passed = (
        scores["fp_rate"] <= thresholds["trigger_fp_rate_max"]
        and scores["fn_rate"] <= thresholds["trigger_fn_rate_max"]
        and scores["functional_pass_rate"] >= thresholds["functional_pass_rate_min"]
        and scores["tokens_p50"] <= thresholds["max_tokens_per_case_p50"]
    )
    print(f"\n{'metric':<22}{'score':>8}   threshold")
    print(f"{'trigger FP rate':<22}{scores['fp_rate']:>8.2f}   <= {thresholds['trigger_fp_rate_max']}")
    print(f"{'trigger FN rate':<22}{scores['fn_rate']:>8.2f}   <= {thresholds['trigger_fn_rate_max']}")
    print(f"{'functional pass@1':<22}{scores['functional_pass_rate']:>8.2f}   >= {thresholds['functional_pass_rate_min']}")
    print(f"{'tokens p50':<22}{int(scores['tokens_p50']):>8d}   <= {thresholds['max_tokens_per_case_p50']}")
    print(f"\n[arize] scores read from experiment {experiment_id}")
    print(f"VERDICT: {'PASS' if passed else 'FAIL'}  ({skill}/{variant}; scored by the Arize Eval Hub)")
    return 0 if passed else 1


def main():
    ap = argparse.ArgumentParser(description="Gate a skill in CI, on Arize.")
    ap.add_argument("--skill", required=True, choices=["api-helper", "story-writer"])
    ap.add_argument("--variant", default=None, help="which skill variant to gate (default: the candidate)")
    ap.add_argument("--mock", action="store_true", help="offline simulator (no tokens)")
    ap.add_argument("--max-cases", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="Eval 0 only, then stop")
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    args = ap.parse_args()

    variant = args.variant or DEFAULT_VARIANT[args.skill]
    sys.exit(gate(args.skill, variant, mock=args.mock, max_cases=args.max_cases,
                  dry_run=args.dry_run, work_root=Path(args.work_root), concurrency=args.concurrency))


if __name__ == "__main__":
    main()
