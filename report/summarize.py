#!/usr/bin/env python3
"""report/summarize.py — the final comparison report (demo beat 5).

Reads the main-split gate output (report/out/gate_<skill>.json, produced by
ci/gate.py) and adds:
  - the skill_off baseline column,
  - Eval 3 pairwise win rate (Skill A variants only),
  - the HOLDOUT pass rate, scored EXACTLY ONCE here (never during iteration,
    this is the honest answer to the overfitting question),
  - the AX experiment-comparison pointer.

Run order:  python ci/gate.py --skill <s> [--mock]   then   python report/summarize.py --skill <s> [--mock]

Pairwise: with --real-judge on real runs, uses the blinded Opus judge
(gates/eval3_pairwise). Offline/mock, it derives winners from functional
outcomes (labeled as such) because mock outputs are trivial. AX's Agent-as-a-Judge
is the documented second method on the same experiment runs.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "gates", REPO / "harness", REPO / "verifiers" / "api_helper"):
    sys.path.insert(0, str(p))

from common import RunResult, load_cases                      # noqa: E402
import eval2_functional as e2                                 # noqa: E402
import eval3_pairwise as e3                                   # noqa: E402
from run_case import run_case, DEFAULT_WORK_ROOT              # noqa: E402

OUT = REPO / "report" / "out"


def _offline_classify(prompt: str, model: str) -> str:
    t = prompt.lower()
    good = "as a" in t and "so that" in t and ("given" in t or "acceptance" in t)
    return json.dumps({"label": "good" if good else "bad", "explanation": "offline heuristic"})


def score_holdout(skill: str, arms: list[str], mock: bool | None, real_judge: bool) -> dict:
    """Run the harness on the holdout split ONCE and score functional pass rate."""
    ho_path = REPO / "datasets" / "holdout" / f"{skill.replace('-', '_')}_cases.jsonl"
    if not ho_path.exists():
        return {}
    cases = load_cases(ho_path)
    classify = None if (real_judge and not mock) else _offline_classify
    out: dict[str, dict] = {}
    for arm in arms + ["skill_off"]:
        passes = n = 0
        for c in cases:
            if not c.get("check"):
                continue
            r = run_case(c, skill, arm, 0, DEFAULT_WORK_ROOT / "holdout", mock=mock)
            fe = e2.evaluate(c, r, classify_fn=classify)
            if fe is not None:
                n += 1
                passes += 1 if fe.passed else 0
        out[arm] = {"pass_rate": (passes / n) if n else 0.0, "n": n}
    return out


def pairwise(skill: str, gate: dict, mock: bool | None, real_judge: bool) -> dict:
    """Eval 3 pairwise (Skill A only)."""
    if skill != "api-helper":
        return {}
    # collect trial-0 outputs per case per arm from the main run set
    outputs: dict[str, dict[str, RunResult]] = {}
    for rd in gate.get("per_run", []):
        r = RunResult.from_dict(rd)
        if r.trial == 0 and r.arm in ("variant_a", "variant_b"):
            outputs.setdefault(r.case_id, {})[r.arm] = r

    cases = {c["id"]: c for c in load_cases(REPO / "datasets" / "api_helper_cases.jsonl")}
    detail = gate.get("functional_detail", {})

    results = []
    for cid, arms in outputs.items():
        if "variant_a" not in arms or "variant_b" not in arms:
            continue
        case = cases.get(cid)
        if not case or not case.get("check"):
            continue
        if real_judge and not mock:
            results.append(e3.judge_case(case, arms["variant_a"], arms["variant_b"]))
        else:
            # derive from functional outcome (mock outputs are trivial)
            a_pass = detail.get(f"{cid}|variant_a|0", {}).get("score", 0) >= 1
            b_pass = detail.get(f"{cid}|variant_b|0", {}).get("score", 0) >= 1
            winner = "variant_b" if (b_pass and not a_pass) else \
                     "variant_a" if (a_pass and not b_pass) else "tie"
            results.append({"case_id": cid, "winner_variant": winner,
                            "score_variant_a": 4.0 if a_pass else 2.0,
                            "score_variant_b": 4.0 if b_pass else 2.0,
                            "rationale": "derived from functional outcome"})
    agg = e3.aggregate(results)
    agg["method"] = "blinded Opus judge" if (real_judge and not mock) else "derived from functional outcomes"
    return agg


def build_markdown(skill: str, gate: dict, holdout: dict, pw: dict) -> str:
    verdicts = gate.get("verdicts", {})
    eff = gate.get("efficiency", {})
    fr = gate.get("functional_rate", {})
    trig = gate.get("trigger", {})
    arms = [a for a in ("skill_off", "variant_a", "variant_b") if a in fr or a in eff]
    have_variants = "variant_a" in verdicts and "variant_b" in verdicts

    def cell(v):
        return "n/a" if v is None else v

    lines = [f"# Skill gate report — `{skill}`\n"]
    cols = "| Metric | " + " | ".join(arms) + (" | Δ (b−a) |" if have_variants else " |")
    sep = "|" + "---|" * (len(arms) + (2 if have_variants else 1))
    lines += [cols, sep]

    def delta(a, b, pct=False):
        if not have_variants:
            return None
        d = b - a
        return f"{d:+.2f}" if not pct else f"{d:+.0f}"

    # functional pass rate
    fvals = {a: fr.get(a, {}).get("pass_rate", None) for a in arms}
    lines.append("| Functional pass@1 (3 trials) | " + " | ".join(
        f"{fvals[a]:.2f}" if fvals[a] is not None else "n/a" for a in arms) +
        (f" | {delta(fvals.get('variant_a',0), fvals.get('variant_b',0))} |" if have_variants else " |"))

    # trigger FP/FN
    def trig_cell(a):
        m = trig.get(a)
        return f"{m['fp_rate']:.2f} / {m['fn_rate']:.2f}" if m else "n/a"
    lines.append("| Trigger FP / FN rate | " + " | ".join(trig_cell(a) for a in arms) +
                 (" |  |" if have_variants else " |"))

    # pairwise win rate
    if have_variants and pw:
        pw_row = {"skill_off": "n/a",
                  "variant_a": f"{pw.get('variant_a_win_rate',0):.2f}",
                  "variant_b": f"{pw.get('variant_b_win_rate',0):.2f}"}
        lines.append("| Pairwise win rate | " + " | ".join(pw_row.get(a, "n/a") for a in arms) + " |  |")

    # tokens
    tvals = {a: eff.get(a, {}).get("tokens_p50", None) for a in arms}
    lines.append("| Tokens p50 / case | " + " | ".join(
        f"{int(tvals[a])}" if tvals[a] is not None else "n/a" for a in arms) +
        (f" | {int(tvals.get('variant_b',0)-tvals.get('variant_a',0)):+d} |" if have_variants else " |"))

    # holdout (scored once)
    if holdout:
        hvals = {a: holdout.get(a, {}).get("pass_rate", None) for a in arms}
        lines.append("| **Holdout pass rate (scored once)** | " + " | ".join(
            f"**{hvals[a]:.2f}**" if hvals[a] is not None else "n/a" for a in arms) +
            (f" | {delta(hvals.get('variant_a',0), hvals.get('variant_b',0))} |" if have_variants else " |"))

    # AX pointer
    lines.append("")
    ax_exp = gate.get("ax_experiments") or {}
    if ax_exp:
        lines.append("**AX experiments:** " + ", ".join(f"`{n}`" for n in ax_exp.values()))
        lines.append("\nOpen the AX console → Experiments to compare these side by side "
                     "(experiment names are SKILL.md content hashes = version history).")
    else:
        lines.append("_AX experiments not pushed (no creds or --no-ax). Local metrics above are authoritative._")
    if pw:
        lines.append(f"\n_Pairwise method: {pw.get('method')}. Second method: AX Agent-as-a-Judge on the same runs._")

    # holdout guard note
    lines.append("\n> Holdout was scored exactly once, here, and never used during iteration "
                 "(guards against overfitting the gate).")
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True)
    ap.add_argument("--mock", action="store_true")
    ap.add_argument("--real-judge", action="store_true")
    args = ap.parse_args()

    gate_path = OUT / f"gate_{args.skill}.json"
    if not gate_path.exists():
        print(f"error: {gate_path} not found — run `python ci/gate.py --skill {args.skill}` first",
              file=sys.stderr)
        sys.exit(2)
    gate = json.loads(gate_path.read_text())

    arms = [a for a in gate.get("verdicts", {})]
    mock = True if args.mock else None
    holdout = score_holdout(args.skill, arms, mock, args.real_judge)
    pw = pairwise(args.skill, gate, mock, args.real_judge)

    md = build_markdown(args.skill, gate, holdout, pw)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / f"summary_{args.skill}.md").write_text(md)
    print(md)
    print(f"\n[written] {OUT / f'summary_{args.skill}.md'}")


if __name__ == "__main__":
    main()
