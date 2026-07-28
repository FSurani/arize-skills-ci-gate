#!/usr/bin/env python3
"""Arize Eval Hub — the evaluators and the Arize plumbing for skill gating.

This is the heart of the demo: the checks that decide whether a skill is good,
expressed as **Arize evaluators** that Arize creates once and runs **server-side**.

  • trigger-accuracy    — did the skill fire exactly when it should? (over-eager = gotcha)
  • output-correctness  — deterministic correctness for verifiable skills
  • output-quality      — LLM judge for non-verifiable skills (INVEST, etc.)
  • token-efficiency    — token budget (works-but-wasteful = gotcha)

Adding a novel eval is ~10 lines: add an entry to EVALUATORS (a code string or an
LLM template) + list it in SKILL_EVALS + map its columns in MAPPINGS.

Public API used by setup.py (provisioning) and ci/gate.py (per-run):
  ensure_evaluators()                  -> {logical: evaluator_id}   (create in Arize, idempotent)
  ensure_dataset(skill)                -> (name, dataset_id)        (one stable dataset per skill)
  run_experiment(skill, variant, runs) -> (dataset_id, experiment_id)  (push + score server-side)
  read_metrics(skill, ds_id, exp_id)   -> {fp_rate, fn_rate, functional_pass_rate, tokens_p50}

(Eval 0 — the structural + security scan — runs first, locally, in ci/gate.py.)
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from pathlib import Path

import requests

REPO = Path(__file__).resolve().parents[1]
DATASETS = REPO / "datasets"
import sys
for p in (REPO / "gates", REPO / "harness"):
    sys.path.insert(0, str(p))
from common import load_cases, detect_triggered              # noqa: E402

# ── Arize connection ─────────────────────────────────────────────────────────
SPACE = os.environ.get("ARIZE_SPACE_ID", "")       # base64 space GID
SA_ANTHROPIC = os.environ.get("ARIZE_AI_INTEGRATION_ID", "")  # provider creds for the LLM judge
MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-4-6")
API = "https://api.arize.com/v2"
KEY = os.environ.get("ARIZE_API_KEY", "")
H = {"Authorization": f"Bearer {KEY}"}
HUB_STATE = REPO / "report" / "out" / "hub_state.json"   # persists the stable dataset id


def P(col: str) -> str:
    # experiment-run columns are addressed by their bare top-level name
    return col


CODE_IMPORTS = ("from typing import Any, Optional\n"
                "from arize.experimental.datasets.experiments.evaluators.base import "
                "EvaluationResult, CodeEvaluator")

# ── evaluator definitions (the evals) ────────────────────────────────────────
# Code evaluators run server-side in Arize's sandbox, so each is a self-contained
# code string (Arize's validator wants a single `return EvaluationResult(...)`).
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
    "trigger":    {"name": "trigger-accuracy",     "kind": "code",     "col": "trigger_accuracy",
                   "code": TRIGGER_CODE,    "variables": ["triggered", "should_trigger"]},
    "verifier":   {"name": "output-correctness",   "kind": "code",     "col": "output_correctness",
                   "code": VERIFIER_CODE,   "variables": ["answer", "expected", "api_log"]},
    "efficiency": {"name": "token-efficiency",     "kind": "code",     "col": "token_efficiency",
                   "code": EFFICIENCY_CODE, "variables": ["tokens"]},
    "rubric":     {"name": "output-quality",       "kind": "template", "col": "output_quality",
                   "template": RUBRIC_TEMPLATE, "choices": {"good": 1, "bad": 0}},
}
# which evaluators apply to each skill (verifiable -> verifier, non-verifiable -> rubric)
SKILL_EVALS = {
    "api-helper":   ["trigger", "verifier", "efficiency"],
    "story-writer": ["trigger", "rubric", "efficiency"],
}
# column mappings per evaluator (output is top-level; the rest are extra columns)
MAPPINGS = {
    "trigger":    lambda: {"triggered": P("triggered"), "should_trigger": P("should_trigger")},
    "verifier":   lambda: {"answer": P("answer"), "expected": P("expected"), "api_log": P("api_log")},
    "efficiency": lambda: {"tokens": P("tokens")},
    "rubric":     lambda: {"output": "output", "rubric": P("rubric")},
}
# functional evaluators score positives only (negatives have no functional task)
QUERY_FILTER = {"verifier": "should_trigger = 'true'", "rubric": "should_trigger = 'true'"}

# expected answers for the api-helper cases (mirrors the mock Orders API seed)
_ANSWERS = {
    "t01": ("count.txt", "5"), "t02": ("customer.txt", "Globex"), "t03": ("new_id.txt", "ord_0005"),
    "t04": ("status.txt", "shipped"), "t05": ("open_count.txt", "3"), "t06": ("sku.txt", "WIDGET-3"),
    "t07": ("ids.txt", "\n".join(f"ord_{i:04d}" for i in range(5))), "t08": ("created_status.txt", "open"),
    "t12": ("max_order.txt", "ord_0003"), "e02": ("status_code.txt", "422"),
    "t09": ("acme.txt", "1"), "t10": ("total_units.txt", "11"), "e01": ("out.txt", "NOT_FOUND"),
}


# ── ax CLI + raw-API helpers ─────────────────────────────────────────────────
def ax(*a):
    return subprocess.run(["ax", *a], capture_output=True, text=True, timeout=300)


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


def _hub_state():
    try:
        return json.loads(HUB_STATE.read_text()) if HUB_STATE.exists() else {}
    except Exception:
        return {}


def _save_hub_state(s):
    HUB_STATE.parent.mkdir(parents=True, exist_ok=True)
    HUB_STATE.write_text(json.dumps(s, indent=2))


def list_evaluators():
    # `evaluators list` caps -l at 100 (422 above that); paginate by cursor.
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


def poll_run(task_id, timeout=240):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = requests.get(f"{API}/tasks/{task_id}/runs", headers=H, timeout=30)
        runs = r.json().get("task_runs", []) if r.status_code == 200 else []
        if runs and runs[0].get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
            return runs[0].get("status")
        time.sleep(5)
    return "TIMEOUT"


# ── provisioning: evaluators + dataset (setup.py) ────────────────────────────
def ensure_evaluators():
    """Create the cc- evaluators in Arize if absent; return {logical: id}.
    Idempotent, and robust to a flaky `evaluators list`."""
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
            found = list_evaluators().get(spec["name"])
            if not found:
                raise RuntimeError(f"could not create or find evaluator {spec['name']}")
            out[logical] = found
            print(f"[evaluator] found existing {spec['name']} -> {found}")
    return out


def ensure_dataset(skill, name=None):
    """Reuse ONE stable dataset per skill (id persisted locally); create once if
    absent. Experiments (over time) are the version history, not new datasets."""
    name = name or skill
    cases = load_cases(DATASETS / f"{skill.replace('-', '_')}_cases.jsonl")
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


# ── per-run: build rows, create experiment, score, read metrics (gate.py) ────
def build_rows(skill, variant, runs, cases_by_id):
    """Experiment rows from harness runs. Every row carries the SAME columns
    (Arize rejects a ragged/null schema), so negatives get empty defaults."""
    rows = []
    for r in runs:
        c = cases_by_id.get(r.case_id, {})
        row = {
            "example_id": None,  # filled after dataset export
            "case_id": r.case_id, "variant": variant,
            "output": r.final_output or "",
            "triggered": bool(detect_triggered(r.tool_spans, skill)),
            "should_trigger": bool(c.get("should_trigger")),
            "tokens": int(r.tokens_total),
        }
        if skill == "api-helper":
            row["answer"], row["expected"], row["api_log"] = "", "", ""
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


def _score_experiment(skill, variant, ds_id, exp_id, ev_ids, ts):
    """Create + trigger one Arize eval task per applicable evaluator; wait for each."""
    for logical in SKILL_EVALS[skill]:
        spec = EVALUATORS[logical]
        ttype = "CODE_EVALUATION" if spec["kind"] == "code" else "TEMPLATE_EVALUATION"
        ev_cfg = {"evaluator_id": ev_ids[logical], "column_mappings": MAPPINGS[logical]()}
        qf = QUERY_FILTER.get(logical)
        t = None
        if qf:  # positives-only where applicable
            t = axj("tasks", "create-evaluation", "-n", f"{skill}-{variant}-{logical}-{ts}", "--task-type", ttype,
                    "--dataset", ds_id, "-s", SPACE, "--experiment-ids", exp_id,
                    "--evaluators", json.dumps([{**ev_cfg, "query_filter": qf}]), "-o", "json")
        if not t:
            t = axj("tasks", "create-evaluation", "-n", f"{skill}-{variant}-{logical}-{ts}-nf", "--task-type", ttype,
                    "--dataset", ds_id, "-s", SPACE, "--experiment-ids", exp_id,
                    "--evaluators", json.dumps([ev_cfg]), "-o", "json")
        if not t:
            print(f"[score] task create FAILED for {logical}")
            continue
        ax("tasks", "trigger-run", t["id"], "--experiment-ids", exp_id)
        print(f"[score] {spec['name']}: {poll_run(t['id'])}")


def run_experiment(skill, variant, runs, ev_ids=None):
    """Push harness runs as an Arize experiment against the stable dataset, then
    let the Eval Hub evaluators score it server-side. Returns (dataset_id, experiment_id)."""
    cases = load_cases(DATASETS / f"{skill.replace('-', '_')}_cases.jsonl")
    cases_by_id = {c["id"]: c for c in cases}
    _, ds_id = ensure_dataset(skill)
    ex = json.loads(ax("datasets", "export", ds_id, "--stdout").stdout or "[]")
    cid_to_exid = {(row.get("additional_properties") or {}).get("case_id"): row["id"] for row in ex}
    ev_ids = ev_ids or ensure_evaluators()

    rows = build_rows(skill, variant, runs, cases_by_id)
    for row in rows:
        row["example_id"] = cid_to_exid.get(row["case_id"], row["case_id"])
    ts = int(time.time())
    exp = axj("experiments", "create", "-n", f"{skill}-{variant}-{ts}", "--dataset", ds_id,
              "-s", SPACE, "-f", tmpfile(rows), "-o", "json")
    if not exp or not exp.get("id"):
        raise RuntimeError(f"experiment create failed for {skill}/{variant}")
    print(f"[experiment] {skill}-{variant}-{ts} -> {exp['id']} ({len(rows)} runs)")
    _score_experiment(skill, variant, ds_id, exp["id"], ev_ids, ts)
    return ds_id, exp["id"]


def read_metrics(skill, ds_id, exp_id):
    """Read the hub eval scores back from Arize and reduce to gate metrics.
    Functional pass-rate counts POSITIVES only (negatives have no functional task)."""
    rows = json.loads(ax("experiments", "export", exp_id, "--dataset", ds_id, "-s", SPACE, "--stdout").stdout or "[]")
    trig_col = EVALUATORS["trigger"]["col"]
    func_col = next((EVALUATORS[l]["col"] for l in SKILL_EVALS[skill] if l in ("verifier", "rubric")), None)

    def _b(v):
        return str(v).strip().lower() in ("true", "1", "yes")

    pos = neg = fp = fn = fpass = ffail = 0
    toks = []
    for r in rows:
        ap = r.get("additional_properties") or {}
        st = _b(ap.get("should_trigger"))
        tl = ap.get(f"eval.{trig_col}.label")
        if st:
            pos += 1
            fn += (tl == "false_negative")
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
        "n": {"pos": pos, "neg": neg, "func": fapp},
    }
