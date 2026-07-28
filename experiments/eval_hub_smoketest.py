#!/usr/bin/env python3
"""cc- eval-hub smoke test.

Proves the Arize Evaluator Hub end-to-end with BOTH kinds referenced by ID and
run server-side by Arize:
  - a CODE evaluator (deterministic, no AI integration): `cc-no-error`
  - an LLM template evaluator (reuses the SA Anthropic integration): `cc-correctness`

Flow: create a tiny dataset -> a small experiment (4 runs with mixed outputs)
-> create both evaluators -> create + trigger one eval task per kind -> poll run
status via the raw /v2 API (the CLI's task-run parse chokes on `failure_reason`)
-> export the experiment and verify both `eval.*` columns landed.

Everything created is prefixed `cc-` (per request). Runs in Fahim's Space, reusing
the existing SA Anthropic integration — nothing new stored.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

import requests

SPACE = "U3BhY2U6NDU1NzI6c01CQQ=="                 # Fahim's Space (base64 GID)
SA_ANTHROPIC = "TGxtSW50ZWdyYXRpb246Mjg0OTpmaHFC"  # existing ANTHROPIC integration
MODEL = "claude-sonnet-4-6"
API = "https://api.arize.com/v2"
KEY = os.environ["ARIZE_API_KEY"]
H = {"Authorization": f"Bearer {KEY}"}

# tiny fixed test set — engineered so both evaluators show mixed labels
QA = [
    ("What is 2+2?",                       "Q: What is 2+2? A: 4"),                 # answered, correct
    ("Capital of France?",                 "Q: Capital of France? A: Paris"),       # answered, correct
    ("Color of the sky on a clear day?",   "Q: Color of the sky on a clear day? A: green"),  # answered, INCORRECT
    ("What is 10 divided by 2?",           "ERROR: computation timed out"),         # REFUSED/error
]

CODE_IMPORTS = """from typing import Any, Optional
from arize.experimental.datasets.experiments.evaluators.base import (
    EvaluationResult,
    CodeEvaluator,
)"""

CODE_SRC = '''class CcNoError(CodeEvaluator):
    """Deterministic: did the agent actually answer (vs. error/refusal/empty)?"""

    def evaluate(self, *, output: Optional[Any] = None, **kwargs: Any) -> EvaluationResult:
        text = "" if output is None else str(output)
        t = text.strip().upper()
        refused = (t == "") or t.startswith(("ERROR", "I CANNOT", "I'M SORRY", "SORRY", "I CAN'T"))
        return EvaluationResult(
            label="refused" if refused else "answered",
            score=0.0 if refused else 1.0,
            explanation=("empty/error/refusal output" if refused else "produced a substantive answer"),
        )
'''

LLM_TEMPLATE = """You are checking whether an answer is correct.

{output}

The text above contains a question and the answer that was given. If the given
answer correctly and factually answers the question, respond "correct".
Otherwise respond "incorrect".

Respond with exactly one of these labels: correct, incorrect"""


def ax(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["ax", *args], capture_output=True, text=True, timeout=300)


def ax_json(*args: str):
    r = ax(*args)
    s = r.stdout or ""
    i = min([x for x in (s.find("{"), s.find("[")) if x >= 0], default=-1)
    if i < 0:
        raise RuntimeError(f"no JSON from `ax {' '.join(args[:3])}…`:\n  out={s[:300]}\n  err={r.stderr[:300]}")
    return json.loads(s[i:])


def write_tmp(rows: list[dict]) -> str:
    fh = tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False)
    for r in rows:
        fh.write(json.dumps(r) + "\n")
    fh.close()
    return fh.name


def poll_run(task_id: str, timeout: int = 240) -> dict:
    """Poll the raw API for the task's latest run until terminal."""
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = requests.get(f"{API}/tasks/{task_id}/runs", headers=H, timeout=30)
        if r.status_code == 200:
            runs = r.json().get("task_runs") or r.json().get("runs") or []
            if runs:
                last = runs[0]
                if last.get("status") in ("COMPLETED", "FAILED", "CANCELLED"):
                    return last
        time.sleep(4)
    return last or {"status": "TIMEOUT"}


def main() -> int:
    print("=== cc- eval-hub smoke test (Fahim's Space) ===")

    # 1) dataset
    ds_rows = [{"q": q, "expected": ""} for q, _ in QA]
    ds = ax_json("datasets", "create", "-n", "cc-eval-hub-test", "-s", SPACE,
                 "-f", write_tmp(ds_rows), "-o", "json")
    ds_id = ds["id"]
    print(f"[dataset] cc-eval-hub-test -> {ds_id}")

    # 2) map each example UUID to its q, then build experiment runs
    ex = json.loads(ax("datasets", "export", ds_id, "--stdout").stdout or "[]")
    q_to_exid = {}
    for row in ex:
        q = (row.get("additional_properties") or {}).get("q") or row.get("q")
        q_to_exid[q] = row.get("id") or row.get("example_id")
    run_rows = [{"example_id": q_to_exid[q], "output": out} for q, out in QA]
    exp = ax_json("experiments", "create", "-n", "cc-evalhub-exp", "--dataset", ds_id,
                  "-s", SPACE, "-f", write_tmp(run_rows), "-o", "json")
    exp_id = exp["id"]
    print(f"[experiment] cc-evalhub-exp -> {exp_id} ({len(run_rows)} runs)")

    # 3) evaluators (CODE + LLM), both cc- prefixed
    code_ev = ax_json("evaluators", "create-code-evaluator", "-n", "cc-no-error", "-s", SPACE,
                      "--commit-message", "v1", "--code-type", "custom",
                      "--code-name", "cc_no_error", "--code", CODE_SRC,
                      "--imports", CODE_IMPORTS, "--variables", '["output"]', "-o", "json")
    code_ev_id = code_ev["id"]
    print(f"[evaluator/code] cc-no-error -> {code_ev_id}")

    llm_ev = ax_json("evaluators", "create-template-evaluator", "-n", "cc-correctness", "-s", SPACE,
                     "--commit-message", "v1", "--template-name", "cc_correctness",
                     "--ai-integration-id", SA_ANTHROPIC, "--model-name", MODEL,
                     "--classification-choices", '{"correct": 1, "incorrect": 0}',
                     "--include-explanations", "--invocation-params", '{"temperature": 0}',
                     "--template", LLM_TEMPLATE, "-o", "json")
    llm_ev_id = llm_ev["id"]
    print(f"[evaluator/llm]  cc-correctness -> {llm_ev_id}")

    # 4) one eval task per kind (task-type differs), scoped to the experiment
    tasks = {}
    for label, ttype, ev_id in [("cc-code-task", "CODE_EVALUATION", code_ev_id),
                                ("cc-llm-task", "TEMPLATE_EVALUATION", llm_ev_id)]:
        evaluators = json.dumps([{"evaluator_id": ev_id, "column_mappings": {"output": "output"}}])
        t = ax_json("tasks", "create-evaluation", "-n", f"{label}-{int(time.time())}",
                    "--task-type", ttype, "--dataset", ds_id, "-s", SPACE,
                    "--experiment-ids", exp_id, "--evaluators", evaluators, "-o", "json")
        tasks[label] = t["id"]
        print(f"[task] {label} ({ttype}) -> {t['id']}")

    # 5) trigger + poll (trigger via CLI; its response parse errors on failure_reason,
    #    but the trigger itself succeeds — we confirm via the raw API)
    for label, task_id in tasks.items():
        ax("tasks", "trigger-run", task_id, "--experiment-ids", exp_id)  # ignore parse-bug exit
        run = poll_run(task_id)
        print(f"[run] {label}: status={run.get('status')} failure={run.get('failure_reason')}")

    # 6) verify both eval columns landed on the experiment
    print("\n=== results (Arize-populated eval columns) ===")
    rows = json.loads(ax("experiments", "export", exp_id, "--dataset", ds_id,
                         "-s", SPACE, "--stdout").stdout or "[]")
    ok = True
    for r in rows:
        ap = r.get("additional_properties") or {}
        ne_label = ap.get("eval.cc_no_error.label")
        co_label = ap.get("eval.cc_correctness.label")
        print(f"  out={str(r.get('output'))[:45]!r:48s} "
              f"cc_no_error={ne_label}  cc_correctness={co_label}")
        if ne_label is None or co_label is None:
            ok = False
    print("\n" + ("✅ BOTH hub evaluators populated scores" if ok
                  else "⚠️  some eval columns missing — check task runs"))
    print("\nCreated (all cc-):")
    print(f"  dataset    cc-eval-hub-test   {ds_id}")
    print(f"  experiment cc-evalhub-exp     {exp_id}")
    print(f"  evaluator  cc-no-error (CODE) {code_ev_id}")
    print(f"  evaluator  cc-correctness (LLM) {llm_ev_id}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
