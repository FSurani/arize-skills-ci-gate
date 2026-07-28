#!/usr/bin/env python3
"""Arize Eval Hub — the evaluators and the Arize plumbing for skill gating.

The checks that decide whether a skill is good, expressed as **Arize evaluators**
that Arize creates once and runs **server-side** (the Eval Hub). Driven entirely
through the **Arize Python SDK** (`arize.ArizeClient`) — no CLI, no subprocess.

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
import sys
import time
from pathlib import Path

from arize import ArizeClient
from arize.experiments.types import ExperimentTaskFieldNames
from arize.tasks.types import TaskType, TaskEvaluatorInput
from arize.evaluators.types import CustomCodeConfig, TemplateConfig, EvaluatorLlmConfig
from arize._generated.api_client.models.invocation_params import InvocationParams
from arize._generated.api_client.models.provider_params import ProviderParams

REPO = Path(__file__).resolve().parents[1]
DATASETS = REPO / "datasets"
for p in (REPO / "gates", REPO / "harness"):
    sys.path.insert(0, str(p))
from common import load_cases, detect_triggered              # noqa: E402

# ── Arize connection ─────────────────────────────────────────────────────────
SPACE = os.environ.get("ARIZE_SPACE_ID", "")                  # base64 space GID
KEY = os.environ.get("ARIZE_API_KEY", "")
AI_INTEGRATION = os.environ.get("ARIZE_AI_INTEGRATION_ID", "")  # provider creds for the LLM judge
MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-4-6")
HUB_STATE = REPO / "report" / "out" / "hub_state.json"        # persists the stable dataset id

_CLIENT = None


def client() -> ArizeClient:
    global _CLIENT
    if _CLIENT is None:
        _CLIENT = ArizeClient(api_key=KEY)
    return _CLIENT


CODE_IMPORTS = ("from typing import Any, Optional\n"
                "from arize.experimental.datasets.experiments.evaluators.base import "
                "EvaluationResult, CodeEvaluator")

# ── evaluator definitions (the evals) ────────────────────────────────────────
# Code evaluators run server-side in Arize's sandbox, so each is a self-contained
# code string (Arize's validator wants a single `return EvaluationResult(...)`).
TRIGGER_CODE = '''class CcTrigger(CodeEvaluator):
    """Did the skill fire exactly when it should have?"""
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
    """Correct answer AND correct auth convention (X-Org-Token). Negatives (no
    expected) self-guard to not_applicable instead of a false fail."""
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
    """Token budget check."""
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
# column mappings per evaluator (output is a top-level task field; the rest are extra columns)
MAPPINGS = {
    "trigger":    lambda: {"triggered": "triggered", "should_trigger": "should_trigger"},
    "verifier":   lambda: {"answer": "answer", "expected": "expected", "api_log": "api_log"},
    "efficiency": lambda: {"tokens": "tokens"},
    "rubric":     lambda: {"output": "output", "rubric": "rubric"},
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


def _hub_state():
    try:
        return json.loads(HUB_STATE.read_text()) if HUB_STATE.exists() else {}
    except Exception:
        return {}


def _save_hub_state(s):
    HUB_STATE.parent.mkdir(parents=True, exist_ok=True)
    HUB_STATE.write_text(json.dumps(s, indent=2))


# ── provisioning: evaluators + dataset (setup.py) ────────────────────────────
def ensure_evaluators():
    """Create the evaluators in Arize if absent; return {logical: id}. Idempotent."""
    c = client()
    existing = {e.name: e.id for e in c.evaluators.list(space=SPACE, limit=100).evaluators}
    out = {}
    for logical, spec in EVALUATORS.items():
        if spec["name"] in existing:
            out[logical] = existing[spec["name"]]
            print(f"[evaluator] reuse {spec['name']} -> {out[logical]}")
            continue
        if spec["kind"] == "code":
            ev = c.evaluators.create_code_evaluator(
                name=spec["name"], space=SPACE, commit_message="v1",
                code_config=CustomCodeConfig(type="custom", name=spec["col"], code=spec["code"],
                                             imports=CODE_IMPORTS, variables=spec["variables"]))
        else:
            ev = c.evaluators.create_template_evaluator(
                name=spec["name"], space=SPACE, commit_message="v1",
                template_config=TemplateConfig(
                    name=spec["col"], template=spec["template"],
                    include_explanations=True, use_function_calling_if_available=False,
                    classification_choices=spec["choices"],
                    llm_config=EvaluatorLlmConfig(
                        ai_integration_id=AI_INTEGRATION, model_name=MODEL,
                        invocation_parameters=InvocationParams(temperature=0),
                        provider_parameters=ProviderParams())))
        out[logical] = ev.id
        print(f"[evaluator] created {spec['name']} -> {ev.id}")
    return out


def ensure_dataset(skill, name=None):
    """Reuse ONE stable dataset per skill (id persisted locally); create once if
    absent. Experiments (over time) are the version history, not new datasets."""
    name = name or skill
    cases = load_cases(DATASETS / f"{skill.replace('-', '_')}_cases.jsonl")
    st = _hub_state()
    ds_id = (st.get("datasets") or {}).get(name)
    if ds_id:
        try:
            client().datasets.get(dataset=ds_id, space=SPACE)
            print(f"[dataset] reuse {name} -> {ds_id}")
            return name, ds_id
        except Exception:
            pass
    ds = client().datasets.create(
        name=name, space=SPACE,
        examples=[{"case_id": c["id"], "prompt": c["prompt"]} for c in cases])
    ds_id = ds.id
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


def run_experiment(skill, variant, runs, ev_ids=None):
    """Push harness runs as an Arize experiment against the stable dataset, then
    let the Eval Hub evaluators score it server-side. Returns (dataset_id, experiment_id)."""
    c = client()
    cases = load_cases(DATASETS / f"{skill.replace('-', '_')}_cases.jsonl")
    cases_by_id = {x["id"]: x for x in cases}
    _, ds_id = ensure_dataset(skill)
    cid_to_exid = {ex.additional_properties["case_id"]: ex.id
                   for ex in c.datasets.list_examples(dataset=ds_id, space=SPACE, all=True).examples}
    ev_ids = ev_ids or ensure_evaluators()

    rows = build_rows(skill, variant, runs, cases_by_id)
    for row in rows:
        row["example_id"] = cid_to_exid.get(row["case_id"], row["case_id"])
    ts = int(time.time())
    exp = c.experiments.create(
        name=f"{skill}-{variant}-{ts}", dataset=ds_id, space=SPACE, experiment_runs=rows,
        task_fields=ExperimentTaskFieldNames(example_id="example_id", output="output"))
    print(f"[experiment] {skill}-{variant}-{ts} -> {exp.id} ({len(rows)} runs)")

    for logical in SKILL_EVALS[skill]:
        spec = EVALUATORS[logical]
        ttype = TaskType.CODE_EVALUATION if spec["kind"] == "code" else TaskType.TEMPLATE_EVALUATION
        te = TaskEvaluatorInput(evaluator_id=ev_ids[logical], column_mappings=MAPPINGS[logical](),
                                query_filter=QUERY_FILTER.get(logical))
        task = c.tasks.create_evaluation_task(
            name=f"{skill}-{variant}-{logical}-{ts}", task_type=ttype, evaluators=[te],
            dataset=ds_id, space=SPACE, experiment_ids=[exp.id])
        run = c.tasks.trigger_run(task=task.id, experiment_ids=[exp.id])
        status = c.tasks.wait_for_run(run_id=run.id, timeout=300)
        print(f"[score] {spec['name']}: {getattr(status, 'status', status)}")
    return ds_id, exp.id


def read_metrics(skill, ds_id, exp_id):
    """Read the hub eval scores back from Arize and reduce to gate metrics.
    Functional pass-rate counts POSITIVES only (negatives have no functional task)."""
    runs = client().experiments.list_runs(experiment=exp_id, dataset=ds_id, space=SPACE, all=True).experiment_runs
    trig_col = EVALUATORS["trigger"]["col"]
    func_col = next((EVALUATORS[l]["col"] for l in SKILL_EVALS[skill] if l in ("verifier", "rubric")), None)

    def _b(v):
        return str(v).strip().lower() in ("true", "1", "yes")

    pos = neg = fp = fn = fpass = ffail = 0
    toks = []
    for r in runs:
        ap = r.additional_properties or {}
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
        try:
            if float(t) >= 0:
                toks.append(float(t))
        except (TypeError, ValueError):
            pass
    toks.sort()
    fapp = fpass + ffail
    return {
        "fp_rate": (fp / neg) if neg else 0.0,
        "fn_rate": (fn / pos) if pos else 0.0,
        "functional_pass_rate": (fpass / fapp) if fapp else 0.0,
        "tokens_p50": (toks[len(toks) // 2] if toks else 0),
        "n": {"pos": pos, "neg": neg, "func": fapp},
    }
