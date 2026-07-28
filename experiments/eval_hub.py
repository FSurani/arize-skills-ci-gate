#!/usr/bin/env python3
"""Run the skill demo through the Arize Evaluator Hub.

Evaluators are CREATED IN ARIZE (cc- prefixed), referenced by ID, and executed
SERVER-SIDE by Arize over the experiments — not computed locally. Covers:

  cc-eval1-trigger    CODE   triggered vs should_trigger        (both skills)
  cc-eval2-verifier   CODE   answer==expected + auth (api log)  (api-helper)
  cc-eval2-rubric     LLM    INVEST rubric judge, {output}/{rubric} (story-writer)
  cc-eval4-efficiency CODE   tokens under budget                (both skills)

Flow per skill: run the harness (mock) -> build enriched experiment rows (the
columns the evaluators read) -> create a cc- dataset + one cc- experiment per arm
-> ensure the evaluators exist -> create+trigger one eval task per applicable
evaluator -> poll via the raw /v2 API -> read the hub scores back.

Everything created is prefixed `cc-`. Runs in Fahim's Space, reusing the existing
SA Anthropic integration.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
for p in (REPO / "gates", REPO / "harness", REPO / "verifiers" / "api_helper"):
    sys.path.insert(0, str(p))
from common import load_cases                                  # noqa: E402
import eval2_functional as e2                                  # noqa: E402
from run_case import run_matrix_async, DEFAULT_WORK_ROOT       # noqa: E402
from common import detect_triggered                            # noqa: E402

SPACE = "U3BhY2U6NDU1NzI6c01CQQ=="                 # Fahim's Space (base64 GID)
SA_ANTHROPIC = "TGxtSW50ZWdyYXRpb246Mjg0OTpmaHFC"  # existing ANTHROPIC integration
MODEL = "claude-sonnet-4-6"
API = "https://api.arize.com/v2"
KEY = os.environ.get("ARIZE_API_KEY", "")
H = {"Authorization": f"Bearer {KEY}"}

# Column-mapping path for EXTRA (non-`output`) run columns. Probe-confirmed:
# experiment-run columns are addressed by their BARE top-level name (a mapping of
# {"expected":"expected"} resolved; "additional_properties.expected" did NOT).
EXTRA = os.environ.get("CC_EXTRA_PREFIX", "")


def P(col: str) -> str:
    return f"{EXTRA}{col}"


CODE_IMPORTS = ("from typing import Any, Optional\n"
                "from arize.experimental.datasets.experiments.evaluators.base import "
                "EvaluationResult, CodeEvaluator")

# ── evaluator definitions (cc- prefixed) ─────────────────────────────────────
# NOTE: Arize's code-structure validator rejects nested functions whose return is
# not an EvaluationResult, so all coercion is inlined and each evaluate() has a
# single `return EvaluationResult(...)`.
TRIGGER_CODE = '''class CcTrigger(CodeEvaluator):
    """Eval 1: did the skill fire exactly when it should have?"""
    def evaluate(self, *, triggered=None, should_trigger=None, **kw) -> EvaluationResult:
        t = str(triggered).strip().lower() in ("true", "1", "yes")
        s = str(should_trigger).strip().lower() in ("true", "1", "yes")
        if t == s:
            lab, sc = "correct", 1.0
        elif t and not s:
            lab, sc = "false_positive", 0.0
        else:
            lab, sc = "false_negative", 0.0
        return EvaluationResult(label=lab, score=sc, explanation="triggered=" + str(t) + " should_trigger=" + str(s))
'''

VERIFIER_CODE = '''class CcVerifier(CodeEvaluator):
    """Eval 2 (verifier): correct answer AND correct auth convention (X-Org-Token).
    Negatives (no expected) self-guard to not_applicable instead of a false fail."""
    def evaluate(self, *, answer=None, expected=None, api_log=None, **kw) -> EvaluationResult:
        a = ("" if answer is None else str(answer)).strip()
        e = ("" if expected is None else str(expected)).strip()
        if not e:
            return EvaluationResult(label="not_applicable", score=1.0,
                                    explanation="no functional check for this case (negative)")
        log = "" if api_log is None else str(api_log)
        auth_ok = ("X-Org-Token" in log)
        answer_ok = a.lower() == e.lower() or e.lower() in a.lower()
        ok = answer_ok and auth_ok
        return EvaluationResult(
            label="pass" if ok else "fail",
            score=1.0 if ok else 0.0,
            explanation=f"answer={a[:40]!r} expected={e!r} answer_ok={answer_ok} auth_ok={auth_ok}",
        )
'''

EFFICIENCY_CODE = '''class CcEfficiency(CodeEvaluator):
    """Eval 4: token budget check."""
    def evaluate(self, *, tokens=None, **kw) -> EvaluationResult:
        try: n = int(float(tokens))
        except Exception: n = 0
        ok = 0 < n <= 30000
        return EvaluationResult(label="efficient" if ok else "over_budget",
                                score=1.0 if ok else 0.0, explanation=f"tokens={n} (budget 30000)")
'''

RUBRIC_TEMPLATE = """You are grading a generated output against a rubric.

[Output]
{output}

[Rubric]
{rubric}

Reply "good" only if the output satisfies the rubric; otherwise "bad".
Respond with exactly one of these labels: good, bad"""

# logical name -> spec
EVALUATORS = {
    "trigger":    {"name": "cc-eval1-trigger",    "kind": "code",     "col": "cc_eval1_trigger",
                   "code": TRIGGER_CODE,    "variables": ["triggered", "should_trigger"]},
    "verifier":   {"name": "cc-eval2-verifier",   "kind": "code",     "col": "cc_eval2_verifier",
                   "code": VERIFIER_CODE,   "variables": ["answer", "expected", "api_log"]},
    "efficiency": {"name": "cc-eval4-efficiency", "kind": "code",     "col": "cc_eval4_efficiency",
                   "code": EFFICIENCY_CODE, "variables": ["tokens"]},
    "rubric":     {"name": "cc-eval2-rubric",     "kind": "template", "col": "cc_eval2_rubric",
                   "template": RUBRIC_TEMPLATE, "choices": {"good": 1, "bad": 0}},
}
SKILL_EVALS = {
    "api-helper":   ["trigger", "verifier", "efficiency"],
    "story-writer": ["trigger", "rubric", "efficiency"],
}
# column_mappings per evaluator (output is top-level; the rest are EXTRA)
MAPPINGS = {
    "trigger":    lambda: {"triggered": P("triggered"), "should_trigger": P("should_trigger")},
    "verifier":   lambda: {"answer": P("answer"), "expected": P("expected"), "api_log": P("api_log")},
    "efficiency": lambda: {"tokens": P("tokens")},
    "rubric":     lambda: {"output": "output", "rubric": P("rubric")},
}
# functional evaluators should only score positives; trigger/efficiency run on all.
# Best-effort filter (falls back to no-filter if the CLI rejects it — the verifier
# also self-guards to not_applicable on negatives regardless).
QUERY_FILTER = {"verifier": "should_trigger = 'true'", "rubric": "should_trigger = 'true'"}

_ANSWERS = {  # (target file, expected answer) — mirrors mockapi seed
    "t01": ("count.txt", "5"), "t02": ("customer.txt", "Globex"), "t03": ("new_id.txt", "ord_0005"),
    "t04": ("status.txt", "shipped"), "t05": ("open_count.txt", "3"), "t06": ("sku.txt", "WIDGET-3"),
    "t07": ("ids.txt", "\n".join(f"ord_{i:04d}" for i in range(5))), "t08": ("created_status.txt", "open"),
    "t12": ("max_order.txt", "ord_0003"), "e02": ("status_code.txt", "422"),
    "t09": ("acme.txt", "1"), "t10": ("total_units.txt", "11"), "e01": ("out.txt", "NOT_FOUND"),
}


# ── ax + raw-API helpers ─────────────────────────────────────────────────────
def ax(*a): return subprocess.run(["ax", *a], capture_output=True, text=True, timeout=300)


def axj(*a):
    s = ax(*a).stdout or ""
    i = min([x for x in (s.find("{"), s.find("[")) if x >= 0], default=-1)
    return json.loads(s[i:]) if i >= 0 else None


def tmpfile(rows):
    f = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for r in rows:
        f.write(json.dumps(r) + "\n")
    f.close()
    return f.name


def list_evaluators():
    # NOTE: `evaluators list` caps -l at 100 (422 above that); paginate by cursor.
    out, cursor = {}, None
    for _ in range(20):
        args = ["evaluators", "list", "--space", SPACE, "-l", "100", "-o", "json"]
        if cursor:
            args += ["-c", cursor]
        d = axj(*args) or {}
        for e in d.get("evaluators", []):
            out[e["name"]] = e["id"]
        cursor = (d.get("pagination") or {}).get("next_cursor")
        if not cursor:
            break
    return out


def ensure_evaluators():
    """Create the cc- evaluators if absent; return {logical: evaluator_id}.
    Robust to a flaky `evaluators list` (falls back to re-listing by name when a
    create returns nothing — e.g. an 'already exists' race)."""
    existing = list_evaluators()
    out = {}
    for logical, spec in EVALUATORS.items():
        if spec["name"] in existing:
            out[logical] = existing[spec["name"]]
            print(f"[evaluator] reuse {spec['name']} -> {out[logical]}")
            continue
        if spec["kind"] == "code":
            ev = axj("evaluators", "create-code-evaluator", "-n", spec["name"], "-s", SPACE,
                     "--commit-message", "v1", "--code-type", "custom", "--code-name", spec["col"],
                     "--code", spec["code"], "--imports", CODE_IMPORTS,
                     "--variables", json.dumps(spec["variables"]), "-o", "json")
        else:
            ev = axj("evaluators", "create-template-evaluator", "-n", spec["name"], "-s", SPACE,
                     "--commit-message", "v1", "--template-name", spec["col"],
                     "--ai-integration-id", SA_ANTHROPIC, "--model-name", MODEL,
                     "--classification-choices", json.dumps(spec["choices"]),
                     "--include-explanations", "--invocation-params", '{"temperature": 0}',
                     "--template", spec["template"], "-o", "json")
        if ev and ev.get("id"):
            out[logical] = ev["id"]
            print(f"[evaluator] created {spec['name']} -> {ev['id']}")
        else:
            found = list_evaluators().get(spec["name"])   # already-exists / flaky-list fallback
            if not found:
                raise RuntimeError(f"could not create or find evaluator {spec['name']}")
            out[logical] = found
            print(f"[evaluator] found existing {spec['name']} -> {found}")
    return out


def poll_run(task_id, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{API}/tasks/{task_id}/runs", headers=H, timeout=30)
        runs = r.json().get("task_runs", []) if r.status_code == 200 else []
        if runs and runs[0].get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            return runs[0].get("status")
        time.sleep(5)
    return "TIMEOUT"


# ── enriched experiment rows ─────────────────────────────────────────────────
def build_rows(skill, arm, runs, cases_by_id):
    """Enriched experiment rows. Every row for a skill carries the SAME columns
    (AX rejects a ragged/null schema), so negatives get empty defaults."""
    rows = []
    for r in runs:
        c = cases_by_id.get(r.case_id, {})
        row = {
            "example_id": None,  # filled after dataset export
            "case_id": r.case_id, "arm": arm,
            "output": r.final_output or "",
            "triggered": bool(detect_triggered(r.tool_spans, skill)),
            "should_trigger": bool(c.get("should_trigger")),
            "tokens": int(r.tokens_total),
        }
        if skill == "api-helper":
            row["answer"], row["expected"], row["api_log"] = "", "", ""  # defaults for negatives
            if r.case_id in _ANSWERS:
                fname, expected = _ANSWERS[r.case_id]
                ws = Path(r.workspace_dir)
                row["answer"] = (ws / fname).read_text().strip() if (ws / fname).exists() else ""
                row["expected"] = expected
                lg = ws / "orders_api_log.jsonl"
                row["api_log"] = lg.read_text() if lg.exists() else ""
        if skill == "story-writer":
            chk = c.get("check") or {}
            row["rubric"] = (chk.get("rubric", "") if isinstance(chk, dict) else "") or "Follow the INVEST criteria."
        rows.append(row)
    return rows


def run_skill(skill, max_cases, concurrency, mock=True):
    import asyncio
    cases = load_cases(REPO / "datasets" / f"{skill.replace('-', '_')}_cases.jsonl")
    if max_cases:
        pos = [c for c in cases if c.get("check")][:max_cases]
        neg = [c for c in cases if not c.get("check")][:max(1, max_cases // 3)]
        cases = pos + neg
    cases_by_id = {c["id"]: c for c in cases}
    # all arms so the hub evals surface the baseline vs variant_a vs variant_b deltas
    arms = {"api-helper": ["skill_off", "variant_a", "variant_b"],
            "story-writer": ["skill_off", "v1"]}[skill]

    # 1) harness — mock (no tokens) or real headless Claude Code (--real)
    triples = [(c, arm, 0) for arm in arms for c in cases]
    print(f"\n[{skill}] harness: {len(triples)} runs ({'mock' if mock else 'REAL'}, concurrency={concurrency})")
    runs = asyncio.run(run_matrix_async(triples, skill, DEFAULT_WORK_ROOT, mock=mock, concurrency=concurrency))
    by_arm = {}
    for r in runs:
        by_arm.setdefault(r.arm, []).append(r)

    # 2) one cc- dataset for the skill
    ts = int(time.time())
    ds_name = f"cc-{skill}-hub-{ts}"
    ds = axj("datasets", "create", "-n", ds_name, "-s", SPACE,
             "-f", tmpfile([{"case_id": c["id"], "prompt": c["prompt"]} for c in cases]), "-o", "json")
    ds_id = ds["id"]
    ex = json.loads(ax("datasets", "export", ds_id, "--stdout").stdout or "[]")
    cid_to_exid = {(row.get("additional_properties") or {}).get("case_id"): row["id"] for row in ex}
    print(f"[{skill}] dataset {ds_name} -> {ds_id} ({len(ex)} examples)")
    ev_ids = ensure_evaluators()

    # 3) one experiment per arm, each scored by the hub evaluators
    exp_ids = {}
    for arm in arms:
        rows = build_rows(skill, arm, by_arm.get(arm, []), cases_by_id)
        for row in rows:
            row["example_id"] = cid_to_exid.get(row["case_id"], row["case_id"])
        exp = axj("experiments", "create", "-n", f"cc-{skill}-{arm}-hub-{ts}", "--dataset", ds_id,
                  "-s", SPACE, "-f", tmpfile(rows), "-o", "json")
        if not exp:
            print(f"[{skill}] experiment create FAILED for arm {arm}")
            continue
        exp_ids[arm] = exp["id"]
        print(f"[{skill}] experiment cc-{skill}-{arm}-hub-{ts} -> {exp['id']} ({len(rows)} runs)")
        _score_experiment(skill, arm, ds_id, exp["id"], ev_ids, ts)

    # 4) per-arm summary
    print(f"\n=== {skill}: hub eval results by arm ===")
    cols = [EVALUATORS[l]["col"] for l in SKILL_EVALS[skill]]
    summary = {}
    for arm, eid in exp_ids.items():
        out = json.loads(ax("experiments", "export", eid, "--dataset", ds_id, "-s", SPACE, "--stdout").stdout or "[]")
        agg = {c: {} for c in cols}
        for r in out:
            ap = r.get("additional_properties") or {}
            for c in cols:
                lab = ap.get(f"eval.{c}.label")
                agg[c][lab] = agg[c].get(lab, 0) + 1
        summary[arm] = agg
        print(f"  [{arm}]")
        for c in cols:
            print(f"     {c.replace('cc_',''):16s} {dict(sorted(agg[c].items(), key=lambda x: str(x[0])))}")
    return {"dataset": ds_id, "experiments": exp_ids, "evaluators": ev_ids, "summary": summary}


# ── stable per-skill dataset + local result store (demo build) ───────────────
HUB_STATE = REPO / "report" / "out" / "hub_state.json"
RUNS_DIR = REPO / "report" / "out" / "hub_runs"


def _hub_state():
    try:
        return json.loads(HUB_STATE.read_text()) if HUB_STATE.exists() else {}
    except Exception:
        return {}


def _save_hub_state(s):
    HUB_STATE.parent.mkdir(parents=True, exist_ok=True)
    HUB_STATE.write_text(json.dumps(s, indent=2))


def ensure_dataset(skill, cases, name=None):
    """Reuse ONE stable cc-{skill} dataset (id persisted locally); create once if
    absent or gone. This is what keeps it to one dataset per skill — experiments
    (real or mock, over time) are the version history, not new datasets."""
    name = name or f"cc-{skill}"
    st = _hub_state()
    ds_id = (st.get("datasets") or {}).get(name)
    if ds_id:
        chk = ax("datasets", "export", ds_id, "--stdout")
        if chk.returncode == 0 and (chk.stdout or "").strip().startswith("["):
            print(f"[dataset] reuse {name} -> {ds_id}")
            return name, ds_id
    ds = axj("datasets", "create", "-n", name, "-s", SPACE,
             "-f", tmpfile([{"case_id": c["id"], "prompt": c["prompt"]} for c in cases]), "-o", "json")
    ds_id = ds["id"]
    st.setdefault("datasets", {})[name] = ds_id
    _save_hub_state(st)
    print(f"[dataset] created {name} -> {ds_id} ({len(cases)} examples)")
    return name, ds_id


def _store_runs(skill, arm, mock, runs):
    """Persist harness results locally (survives sandbox teardown) so a run is
    reproducible and auditable before/after the AX push."""
    tag = "mock" if mock else "real"
    for r in runs:
        d = RUNS_DIR / skill / f"{arm}-{tag}" / r.case_id
        d.mkdir(parents=True, exist_ok=True)
        (d / f"trial_{r.trial}.json").write_text(
            json.dumps(r.to_dict() if hasattr(r, "to_dict") else vars(r), indent=2, default=str))


def build_state(skill, specs, dataset_name=None, concurrency=6):
    """Build a clean demo state in ONE stable dataset. `specs` = list of
    {arm, mock, cases}. Runs the harness, stores results locally, pushes one
    experiment per spec to AX, and hub-scores each. Returns {dataset, experiments}."""
    import asyncio
    all_cases = load_cases(REPO / "datasets" / f"{skill.replace('-', '_')}_cases.jsonl")
    cases_by_id = {c["id"]: c for c in all_cases}
    dsname, ds_id = ensure_dataset(skill, all_cases, dataset_name)
    ex = json.loads(ax("datasets", "export", ds_id, "--stdout").stdout or "[]")
    cid_to_exid = {(r.get("additional_properties") or {}).get("case_id"): r["id"] for r in ex}
    ev_ids = ensure_evaluators()
    ts = int(time.time())
    out_exps = {}
    for spec in specs:
        arm, mock, cases = spec["arm"], spec["mock"], spec["cases"]
        tag = "mock" if mock else "real"
        label = f"{arm}-{tag}"
        print(f"\n[{skill}] {label}: {len(cases)} case(s) ({tag})")
        triples = [(c, arm, 0) for c in cases]
        runs = asyncio.run(run_matrix_async(triples, skill, DEFAULT_WORK_ROOT, mock=mock, concurrency=concurrency))
        _store_runs(skill, arm, mock, runs)
        rows = build_rows(skill, arm, runs, cases_by_id)
        for row in rows:
            row["example_id"] = cid_to_exid.get(row["case_id"], row["case_id"])
        exp = axj("experiments", "create", "-n", f"cc-{skill}-{label}-{ts}", "--dataset", ds_id,
                  "-s", SPACE, "-f", tmpfile(rows), "-o", "json")
        if not exp:
            print(f"[{skill}] experiment create FAILED for {label}")
            continue
        out_exps[label] = exp["id"]
        print(f"[{skill}] experiment cc-{skill}-{label}-{ts} -> {exp['id']} ({len(rows)} runs)")
        _score_experiment(skill, arm, ds_id, exp["id"], ev_ids, ts)
    print(f"\n[build_state] {skill}: dataset {dsname}={ds_id}; experiments={out_exps}")
    return {"dataset": ds_id, "dataset_name": dsname, "experiments": out_exps}


def _score_experiment(skill, arm, ds_id, exp_id, ev_ids, ts):
    """Create + trigger one eval task per applicable evaluator over an experiment."""
    for logical in SKILL_EVALS[skill]:
        spec = EVALUATORS[logical]
        ttype = "CODE_EVALUATION" if spec["kind"] == "code" else "TEMPLATE_EVALUATION"
        ev_cfg = {"evaluator_id": ev_ids[logical], "column_mappings": MAPPINGS[logical]()}
        qf = QUERY_FILTER.get(logical)
        t = None
        if qf:  # try positives-only first
            t = axj("tasks", "create-evaluation", "-n", f"cc-{skill}-{arm}-{logical}-{ts}", "--task-type", ttype,
                    "--dataset", ds_id, "-s", SPACE, "--experiment-ids", exp_id,
                    "--evaluators", json.dumps([{**ev_cfg, "query_filter": qf}]), "-o", "json")
        if not t:  # no filter, or filter rejected -> fall back
            t = axj("tasks", "create-evaluation", "-n", f"cc-{skill}-{arm}-{logical}-{ts}-nf", "--task-type", ttype,
                    "--dataset", ds_id, "-s", SPACE, "--experiment-ids", exp_id,
                    "--evaluators", json.dumps([ev_cfg]), "-o", "json")
        if not t:
            print(f"[{skill}] task create FAILED for {logical}")
            continue
        ax("tasks", "trigger-run", t["id"], "--experiment-ids", exp_id)  # parse-bug tolerated
        st = poll_run(t["id"])
        print(f"[{skill}] {spec['name']}: run={st}")

    # 5) read hub scores back
    print(f"\n=== {skill}: hub eval results ===")
    out = json.loads(ax("experiments", "export", exp_id, "--dataset", ds_id, "-s", SPACE, "--stdout").stdout or "[]")
    cols = [EVALUATORS[l]["col"] for l in SKILL_EVALS[skill]]
    for r in out:
        ap = r.get("additional_properties") or {}
        labs = {c: ap.get(f"eval.{c}.label") for c in cols}
        print(f"  {ap.get('case_id'):5s} " + "  ".join(f"{c.replace('cc_','')}={labs[c]}" for c in cols))
    return {"dataset": ds_id, "experiment": exp_id, "evaluators": ev_ids}


def _demo_specs(skill, real_n):
    """The clean demo build: real skill_off+variant_b (a few cases, for beat-2
    traces + real proof) + a full mock arm set (beat-3 engineered deltas), all in
    ONE stable dataset."""
    cases = load_cases(REPO / "datasets" / f"{skill.replace('-', '_')}_cases.jsonl")
    positives = [c for c in cases if c.get("check")]
    real_cases = positives[:real_n]
    if skill == "api-helper":
        return [
            {"arm": "skill_off", "mock": False, "cases": real_cases},
            {"arm": "variant_b", "mock": False, "cases": real_cases},
            {"arm": "variant_a", "mock": True,  "cases": cases},
            {"arm": "variant_b", "mock": True,  "cases": cases},
            {"arm": "skill_off", "mock": True,  "cases": cases},
        ]
    return [  # story-writer
        {"arm": "v1",        "mock": False, "cases": real_cases},
        {"arm": "skill_off", "mock": True,  "cases": cases},
    ]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True, choices=["api-helper", "story-writer"])
    ap.add_argument("--max-cases", type=int, default=None)
    ap.add_argument("--concurrency", type=int, default=6)
    ap.add_argument("--real", action="store_true", help="real headless Claude Code harness (spends tokens)")
    ap.add_argument("--build-demo", action="store_true",
                    help="build the clean single-dataset demo state (real subset + mock full)")
    ap.add_argument("--real-cases", type=int, default=5, help="how many real cases per real arm (--build-demo)")
    args = ap.parse_args()
    if args.build_demo:
        res = build_state(args.skill, _demo_specs(args.skill, args.real_cases), concurrency=args.concurrency)
    else:
        res = run_skill(args.skill, args.max_cases, args.concurrency, mock=not args.real)
    print("\n[done]", json.dumps(res, indent=2))


if __name__ == "__main__":
    main()
