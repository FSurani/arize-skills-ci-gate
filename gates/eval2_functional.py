"""Eval 2 — Functional gating (dual path). The key the customer feature.

Dispatch on `case["check"]["type"]`:
  - "verifier": run verifiers/<skill>/<ref>.py against the final sandbox state
                (mock-API request log + output files). Deterministic.
  - "rubric":   LLM judge scores the run's final output against the case's rubric
                text (single classify call, good/bad rail + explanation).

BOTH paths return the SAME `EvaluationResult` schema, so the CI gate and the
report never branch on which path ran (plan §5). Negatives (`check is None`) have
no functional score and are skipped here — they are Eval 1's concern.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvaluationResult, RunResult  # noqa: E402
from judge import rubric_judge  # noqa: E402

REPO = Path(__file__).resolve().parents[1]


def _skill_pkg(skill: str) -> str:
    """Map skill name (hyphens) to its verifiers package dir (underscores)."""
    return skill.replace("-", "_")


def _load_verifier(skill: str, ref: str) -> Callable:
    pkg_dir = REPO / "verifiers" / _skill_pkg(skill)
    mod_path = pkg_dir / f"{ref}.py"
    if not mod_path.exists():
        raise FileNotFoundError(f"no verifier for {skill}/{ref} at {mod_path}")
    # ensure the shim's `from _util import ...` resolves
    if str(pkg_dir) not in sys.path:
        sys.path.insert(0, str(pkg_dir))
    spec = importlib.util.spec_from_file_location(f"verifier_{skill}_{ref}", mod_path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.verify


def evaluate(
    case: dict,
    run: RunResult,
    classify_fn: Callable[[str, str], str] | None = None,
) -> EvaluationResult | None:
    check = case.get("check")
    if not check:
        return None  # negative case — no functional gate

    ctype = check.get("type")
    if ctype == "verifier":
        verify = _load_verifier(run.skill, check["ref"])
        return verify(run.workspace_dir)

    if ctype == "rubric":
        return rubric_judge(run.final_output, check["rubric"], classify_fn=classify_fn)

    raise ValueError(f"unknown check.type: {ctype!r}")


if __name__ == "__main__":
    import argparse
    import json

    ap = argparse.ArgumentParser(description="Run Eval 2 on a single result.json")
    ap.add_argument("case_json", help="the case row (JSON)")
    ap.add_argument("result_json", help="path to result.json")
    args = ap.parse_args()
    case = json.loads(Path(args.case_json).read_text()) if Path(args.case_json).exists() \
        else json.loads(args.case_json)
    run = RunResult.load(args.result_json)
    res = evaluate(case, run)
    print(json.dumps(res.to_dict() if res else {"skipped": True}, indent=2))
