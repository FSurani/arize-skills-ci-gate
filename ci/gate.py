#!/usr/bin/env python3
"""ci/gate.py — single CI entrypoint. Runs Evals 0-4 on a skill and exits
nonzero on any threshold breach (plan §7).

Sequence:
  Eval 0 (structural + security) per variant  →  short-circuit on failure
  → run the harness locally over the skill's dataset (3 trials/positive,
    1/negative, + a one-time skill_off baseline)
  → Eval 1 (trigger), Eval 2 (functional, dual-path), Eval 4 (efficiency)
  → (DEFAULT) source those eval scores from AX: create one cc- experiment per
    arm, let the hub evaluators score it server-side, read eval.cc_* back, and
    threshold on the AX scores → verdict table → exit 0/1. AX is the system of
    record end-to-end (consistent with the experiment/trace demo beats).

Design note (Option 3+): the DEFAULT path thresholds against the AX hub scores,
so CI consumes AX as the system of record rather than recomputing locally. A
LOCAL threshold compute (--local) is kept as an offline / CI-resilience fallback
and is used automatically if the AX round-trip fails. The legacy best-effort
non-cc `skill@hash` push is retired.

Flags (cost control, plan §12): --mock, --max-cases, --dry-run, --local.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import yaml

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "gates", REPO / "harness", REPO / "experiments", REPO / "verifiers" / "api_helper"):
    sys.path.insert(0, str(p))

from common import RunResult, load_cases                      # noqa: E402
import eval0_structural as e0                                 # noqa: E402
import eval1_trigger as e1                                    # noqa: E402
import eval2_functional as e2                                 # noqa: E402
import eval4_efficiency as e4                                 # noqa: E402
from run_case import run_matrix_async, DEFAULT_WORK_ROOT      # noqa: E402
import run_experiment as ax                                   # noqa: E402

OUT_DIR = REPO / "report" / "out"


# ─────────────────────────────────────────────────────────────────────────────
# skill topology
# ─────────────────────────────────────────────────────────────────────────────
def variants_for(skill: str) -> list[tuple[str, Path]]:
    """(arm, dir-for-eval0) pairs. Two variants for A/B skills, else single arm."""
    base = REPO / "skills" / skill
    if (base / "variant_a").exists():
        return [("variant_a", base / "variant_a"), ("variant_b", base / "variant_b")]
    if (base / "SKILL.md").exists():
        return [("v1", base)]
    raise FileNotFoundError(f"no skill at {base}")


def dataset_path(skill: str) -> Path:
    return REPO / "datasets" / f"{skill.replace('-', '_')}_cases.jsonl"


def load_thresholds(skill: str) -> dict:
    cfg = yaml.safe_load((REPO / "ci" / "thresholds.yaml").read_text())
    merged = dict(cfg.get("default", {}))
    merged.update((cfg.get("skills", {}) or {}).get(skill, {}) or {})
    return merged


# ─────────────────────────────────────────────────────────────────────────────
# offline rubric judge (no tokens) — used when mock/no key unless --real-judge
# ─────────────────────────────────────────────────────────────────────────────
def _offline_classify(prompt: str, model: str) -> str:
    text = prompt.lower()
    good = ("as a" in text and "so that" in text
            and ("given" in text or "acceptance" in text))
    return json.dumps({"label": "good" if good else "bad",
                       "explanation": "offline heuristic INVEST check"})


# ─────────────────────────────────────────────────────────────────────────────
# harness matrix
# ─────────────────────────────────────────────────────────────────────────────
def build_matrix(cases: list[dict], arms: list[str], with_baseline: bool):
    """Yield (case, arm, trial). 3 trials/positive, 1/negative; skill_off baseline
    on positives only, 1 trial."""
    for arm in arms:
        for c in cases:
            positive = bool(c.get("check"))
            n = 3 if positive else 1
            for t in range(n):
                yield c, arm, t
    if with_baseline:
        for c in cases:
            if c.get("check"):
                yield c, "skill_off", 0


# ─────────────────────────────────────────────────────────────────────────────
# gate
# ─────────────────────────────────────────────────────────────────────────────
def run_gate(skill: str, *, arm_filter: str | None, mock: bool | None,
             max_cases: int | None, dry_run: bool, no_ax: bool,
             real_judge: bool, work_root: Path, concurrency: int = 4) -> int:
    thr = load_thresholds(skill)
    variants = variants_for(skill)
    print(f"\n=== GATE: {skill} ===")

    # ── Eval 0 (structural + security), per variant — short-circuit ──────────
    eval0 = {}
    for arm, sdir in variants:
        res = e0.evaluate_skill(sdir)
        eval0[arm] = res
        mark = "PASS" if res.passed else "FAIL"
        print(f"[eval0] {arm:11s} {mark}  {'' if res.passed else res.explanation[:120]}")
    surviving = [(arm, d) for arm, d in variants if eval0[arm].passed]
    if arm_filter:
        surviving = [(a, d) for a, d in surviving if a == arm_filter]
    if not surviving:
        print("\nVERDICT: FAIL — no variant passed Eval 0 (bounced at the door).")
        _save_report(skill, {"eval0": {a: r.to_dict() for a, r in eval0.items()},
                             "verdict": "fail", "stage": "eval0"})
        return 1

    if dry_run:
        print("\n[dry-run] Eval 0 passed for:", [a for a, _ in surviving])
        print("[dry-run] would run harness + Evals 1,2,4 + thresholds; exiting 0.")
        return 0

    # ── harness runs ─────────────────────────────────────────────────────────
    cases = load_cases(dataset_path(skill))
    if max_cases:
        # keep a mix: positives first, then negatives
        pos = [c for c in cases if c.get("check")][:max_cases]
        neg = [c for c in cases if not c.get("check")][:max(1, max_cases // 3)]
        cases = pos + neg
    arms = [a for a, _ in surviving]
    classify = None if (real_judge and not mock) else _offline_classify

    import asyncio
    case_by_id = {c["id"]: c for c in cases}
    triples = list(build_matrix(cases, arms, with_baseline=True))
    print(f"\n[harness] running {len(triples)} runs ({'mock' if mock else 'auto'}), "
          f"concurrency={concurrency}...")
    runs = asyncio.run(run_matrix_async(triples, skill, work_root, mock=mock,
                                        concurrency=concurrency))
    functional: dict = {}      # (case_id, arm, trial) -> EvaluationResult
    for r in runs:
        fe = e2.evaluate(case_by_id[r.case_id], r, classify_fn=classify)
        if fe is not None:
            functional[(r.case_id, r.arm, r.trial)] = fe
    print(f"[harness] done ({len(runs)} runs).")

    # ── Eval 1 / Eval 4 ──────────────────────────────────────────────────────
    trig = e1.metrics_by_arm(cases, runs, skill)
    eff = e4.metrics_by_arm(runs)
    triggered_map = {(rec["case_id"], rec["arm"], rec["trial"]): rec["triggered"]
                     for rec in e1.per_run(cases, runs, skill)}

    # functional pass rate per arm over task/edge runs
    func_rate: dict[str, dict] = {}
    for arm in arms + ["skill_off"]:
        keys = [k for k in functional if k[1] == arm]
        passes = sum(1 for k in keys if functional[k].passed)
        func_rate[arm] = {"pass_rate": (passes / len(keys)) if keys else 0.0,
                          "n": len(keys), "passes": passes}

    # ── verdicts vs thresholds ───────────────────────────────────────────────
    verdicts = {}
    for arm in arms:
        m1 = trig.get(arm, {"fp_rate": 0.0, "fn_rate": 0.0})
        m4 = eff.get(arm, {"tokens_p50": 0})
        fr = func_rate[arm]["pass_rate"]
        checks = {
            "eval0": eval0[arm].passed,
            "trigger_fp": m1["fp_rate"] <= thr["trigger_fp_rate_max"],
            "trigger_fn": m1["fn_rate"] <= thr["trigger_fn_rate_max"],
            "functional": fr >= thr["functional_pass_rate_min"],
            "efficiency": m4["tokens_p50"] <= thr["max_tokens_per_case_p50"],
        }
        verdicts[arm] = {
            "pass": all(checks.values()), "checks": checks,
            "fp_rate": m1["fp_rate"], "fn_rate": m1["fn_rate"],
            "functional_pass_rate": fr, "tokens_p50": m4["tokens_p50"],
        }

    _print_table(skill, arms, verdicts, func_rate, thr)

    # ── AX experiments (best-effort, system-of-record) ───────────────────────
    ax_experiments = {}
    if not no_ax:
        ax_experiments = ax.push_experiments(skill, cases, runs, functional, triggered_map)

    # ── final exit code ──────────────────────────────────────────────────────
    if arm_filter:
        passed = verdicts.get(arm_filter, {}).get("pass", False)
        gated = [arm_filter]
    else:
        passed = any(v["pass"] for v in verdicts.values())
        gated = arms
    print(f"\nVERDICT: {'PASS' if passed else 'FAIL'} "
          f"(gated arm(s): {gated}; pass if {'that arm' if arm_filter else 'any variant'} clears all thresholds)")

    _save_report(skill, {
        "thresholds": thr,
        "eval0": {a: r.to_dict() for a, r in eval0.items()},
        "verdicts": verdicts,
        "trigger": trig,
        "efficiency": eff,
        "functional_rate": func_rate,
        "ax_experiments": ax_experiments,
        "per_run": [r.to_dict() for r in runs],
        "functional_detail": {"|".join(map(str, k)): v.to_dict() for k, v in functional.items()},
        "verdict": "pass" if passed else "fail",
    })
    return 0 if passed else 1


def _metrics_from_ax_rows(rows: list[dict], skill: str) -> dict:
    """Compute the four gated metrics from an AX experiment export, using the
    HUB evaluator labels (eval.cc_*) — i.e. AX is the source of truth, not a
    local recompute. Trigger FP/FN use per-row should_trigger; functional uses
    the verifier OR rubric label; tokens_p50 is the median of the tokens col."""
    import eval_hub as hub
    trig_col = hub.EVALUATORS["trigger"]["col"]                 # cc_eval1_trigger
    func_col = next((hub.EVALUATORS[l]["col"] for l in hub.SKILL_EVALS[skill]
                     if l in ("verifier", "rubric")), None)
    def _b(v):
        return str(v).strip().lower() in ("true", "1", "yes")
    pos = neg = fp = fn = fpass = ffail = 0
    toks: list[float] = []
    for r in rows:
        ap = r.get("additional_properties") or {}
        st = _b(ap.get("should_trigger"))
        tl = ap.get(f"eval.{trig_col}.label")
        if st:
            pos += 1
            fn += (tl == "false_negative")
            # functional gating applies to POSITIVES only (task/edge). Negatives
            # have no functional task; the verifier labels them fail, so counting
            # them would deflate the pass rate (mirrors the local path, where
            # e2.evaluate returns None for non-applicable cases).
            if func_col:
                fl = ap.get(f"eval.{func_col}.label")
                if fl in ("pass", "good"):
                    fpass += 1
                elif fl in ("fail", "bad"):
                    ffail += 1
        else:
            neg += 1
            fp += (tl == "false_positive")
        t = ap.get("tokens")
        if isinstance(t, (int, float)) and t >= 0:
            toks.append(t)
    toks.sort()
    fapp = fpass + ffail
    return {
        "fp_rate": (fp / neg) if neg else 0.0,
        "fn_rate": (fn / pos) if pos else 0.0,
        "functional_pass_rate": (fpass / fapp) if fapp else 0.0,
        "tokens_p50": (toks[len(toks) // 2] if toks else 0),
        "_n": {"pos": pos, "neg": neg, "func_applicable": fapp},
    }


def run_gate_ax(skill: str, *, arm_filter: str | None, mock: bool | None,
                max_cases: int | None, dry_run: bool, work_root: Path,
                concurrency: int = 4) -> int:
    """Option 3+ default path: Eval 0 (local) → create one cc- experiment per arm
    and let the AX hub evaluators score it → read the hub scores back from AX and
    threshold on them. Raises on any AX failure so main() can fall back to local."""
    thr = load_thresholds(skill)
    variants = variants_for(skill)
    print(f"\n=== GATE (AX system-of-record): {skill} ===")

    # ── Eval 0 (structural + security), per variant — short-circuit ──────────
    eval0 = {}
    for arm, sdir in variants:
        res = e0.evaluate_skill(sdir)
        eval0[arm] = res
        print(f"[eval0] {arm:11s} {'PASS' if res.passed else 'FAIL'}  "
              f"{'' if res.passed else res.explanation[:120]}")
    surviving = [(a, d) for a, d in variants if eval0[a].passed]
    if arm_filter:
        surviving = [(a, d) for a, d in surviving if a == arm_filter]
    if not surviving:
        print("\nVERDICT: FAIL — no variant passed Eval 0 (bounced at the door).")
        _save_report(skill, {"eval0": {a: r.to_dict() for a, r in eval0.items()},
                             "verdict": "fail", "stage": "eval0"})
        return 1
    if dry_run:
        print("\n[dry-run] Eval 0 passed for:", [a for a, _ in surviving])
        print("[dry-run] would create cc- experiments, hub-score in AX, threshold; exiting 0.")
        return 0

    # ── create cc- experiments + hub scoring in AX (reuses eval_hub) ─────────
    import eval_hub as hub
    res = hub.run_skill(skill, max_cases, concurrency, mock=(mock is True))
    exp_ids = res.get("experiments") or {}
    ds_id = res.get("dataset")
    if not exp_ids or not ds_id:
        raise RuntimeError("eval_hub produced no experiments (AX unavailable?)")

    # ── read hub scores back from AX and threshold on them ───────────────────
    gated_arms = [a for a, _ in surviving]           # variant arms only (not skill_off)
    verdicts = {}
    for arm in gated_arms:
        eid = exp_ids.get(arm)
        if not eid:
            continue
        out = hub.ax("experiments", "export", eid, "--dataset", ds_id,
                     "-s", hub.SPACE, "--stdout").stdout or "[]"
        m = _metrics_from_ax_rows(json.loads(out), skill)
        checks = {
            "eval0": eval0[arm].passed,
            "trigger_fp": m["fp_rate"] <= thr["trigger_fp_rate_max"],
            "trigger_fn": m["fn_rate"] <= thr["trigger_fn_rate_max"],
            "functional": m["functional_pass_rate"] >= thr["functional_pass_rate_min"],
            "efficiency": m["tokens_p50"] <= thr["max_tokens_per_case_p50"],
        }
        verdicts[arm] = {
            "pass": all(checks.values()), "checks": checks,
            "fp_rate": m["fp_rate"], "fn_rate": m["fn_rate"],
            "functional_pass_rate": m["functional_pass_rate"],
            "tokens_p50": m["tokens_p50"],
        }
    if not verdicts:
        raise RuntimeError("no gated-arm scores read back from AX")

    arms = [a for a in gated_arms if a in verdicts]
    _print_table(skill, arms, verdicts, None, thr)
    print("\n[ax] thresholds sourced from cc- experiments: "
          + ", ".join(f"{a}={exp_ids[a]}" for a in arms))

    if arm_filter:
        passed = verdicts.get(arm_filter, {}).get("pass", False)
        gated = [arm_filter]
    else:
        passed = any(v["pass"] for v in verdicts.values())
        gated = arms
    print(f"\nVERDICT: {'PASS' if passed else 'FAIL'} "
          f"(source: AX hub scores; gated arm(s): {gated}; "
          f"pass if {'that arm' if arm_filter else 'any variant'} clears all thresholds)")
    _save_report(skill, {
        "thresholds": thr, "source": "ax_hub",
        "eval0": {a: r.to_dict() for a, r in eval0.items()},
        "verdicts": verdicts, "ax_experiments": exp_ids, "ax_dataset": ds_id,
        "verdict": "pass" if passed else "fail",
    })
    return 0 if passed else 1


def _print_table(skill, arms, verdicts, func_rate, thr):
    print(f"\n{'metric':<24}" + "".join(f"{a:>14}" for a in arms))
    def row(label, fmt):
        print(f"{label:<24}" + "".join(f"{fmt(a):>14}" for a in arms))
    row("trigger FP rate", lambda a: f"{verdicts[a]['fp_rate']:.2f}")
    row("trigger FN rate", lambda a: f"{verdicts[a]['fn_rate']:.2f}")
    row("functional pass@1", lambda a: f"{verdicts[a]['functional_pass_rate']:.2f}")
    row("tokens p50", lambda a: f"{int(verdicts[a]['tokens_p50'])}")
    row("VERDICT", lambda a: "PASS" if verdicts[a]["pass"] else "FAIL")
    print(f"\nthresholds: FP<={thr['trigger_fp_rate_max']} FN<={thr['trigger_fn_rate_max']} "
          f"func>={thr['functional_pass_rate_min']} tokens_p50<={thr['max_tokens_per_case_p50']}")


def _save_report(skill: str, data: dict):
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / f"gate_{skill}.json").write_text(json.dumps(data, indent=2, default=str))


def main():
    ap = argparse.ArgumentParser(description="Run the skill gate (Evals 0-4).")
    ap.add_argument("--skill", required=True)
    ap.add_argument("--arm", default=None, help="gate only this variant (e.g. variant_b)")
    ap.add_argument("--mock", action="store_true", help="force the offline simulator (no tokens)")
    ap.add_argument("--max-cases", type=int, default=None)
    ap.add_argument("--dry-run", action="store_true", help="Eval 0 only, then stop")
    ap.add_argument("--local", "--no-ax", dest="local", action="store_true",
                    help="threshold on LOCALLY computed evals (offline / CI-resilience "
                         "fallback) instead of the AX hub scores")
    ap.add_argument("--real-judge", action="store_true", help="use the real LLM judge (spends tokens)")
    ap.add_argument("--concurrency", type=int, default=int(os.environ.get("SKILLS_EVAL_CONCURRENCY", "4")),
                    help="how many harness runs execute in parallel (asyncio)")
    ap.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    args = ap.parse_args()

    mock = True if args.mock else None

    # DEFAULT: threshold on AX hub scores (system of record). Fall back to the
    # local compute on --local or any AX failure (offline / CI resilience).
    if not args.local:
        try:
            code = run_gate_ax(args.skill, arm_filter=args.arm, mock=mock,
                               max_cases=args.max_cases, dry_run=args.dry_run,
                               work_root=Path(args.work_root), concurrency=args.concurrency)
            sys.exit(code)
        except Exception as e:  # noqa: BLE001
            print(f"\n[ax] gate via AX failed ({e!r}); "
                  f"falling back to LOCAL threshold compute.", file=sys.stderr)

    code = run_gate(args.skill, arm_filter=args.arm, mock=mock,
                    max_cases=args.max_cases, dry_run=args.dry_run,
                    no_ax=True, real_judge=args.real_judge,
                    work_root=Path(args.work_root), concurrency=args.concurrency)
    sys.exit(code)


if __name__ == "__main__":
    main()
