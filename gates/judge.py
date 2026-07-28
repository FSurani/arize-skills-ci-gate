"""LLM judges used by Eval 2 (rubric path) and Eval 3 (pairwise).

Judge model = Opus (`claude-opus-4-8`) — judging is one cheap call
per run, so quality is worth it. Temperature 0, single classify call, rails +
explanation (`provide_explanation` is load-bearing: explanations feed the report,
demo beat 4, and the stretch optimizer).

Harness-agnostic: this imports the Anthropic API SDK only — never Claude Code /
Agent SDK symbols. Both judges accept an injectable `classify_fn`
so the blinding/unit tests can run without spending tokens or hitting the network.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvaluationResult  # noqa: E402

JUDGE_MODEL = os.environ.get("JUDGE_MODEL", "claude-sonnet-4-6")

# ── prompt templates ─────────────────────────────────────────────────────────
RUBRIC_TEMPLATE = """You are judging a generated output against the rubric below.
    [BEGIN DATA]
    [Generated Output]: {output}
    [Rubric]: {rubric}
    [END DATA]
Judge whether the output satisfies the rubric: faithful (no invented facts,
correct attribution), complete on the rubric's key points, and well-formed.
First think briefly, then decide.

Reply with ONLY a JSON object: {{"label": "good" | "bad", "explanation": "<one sentence>"}}"""

PAIRWISE_TEMPLATE = """You are blindly comparing two candidate outputs (A and B) for the same task.
You do NOT know which system produced which; judge only on quality.

[TASK]
{task}

[OUTPUT A]
{output_a}

[OUTPUT B]
{output_b}

Decide which output is better overall (correctness, completeness, clarity). Also
give each an absolute quality score from 1 (poor) to 5 (excellent).

Reply with ONLY a JSON object:
{{"winner": "A" | "B" | "tie", "score_a": <1-5>, "score_b": <1-5>, "rationale": "<one sentence>"}}"""


# ── Anthropic-backed classify (default) ──────────────────────────────────────
def _anthropic_classify(prompt: str, model: str) -> str:
    """One message call at temperature 0; returns raw text."""
    import anthropic  # imported lazily so tests needn't have it

    client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY
    resp = client.messages.create(
        model=model,
        max_tokens=512,
        temperature=0,
        messages=[{"role": "user", "content": prompt}],
    )
    return "".join(block.text for block in resp.content if getattr(block, "type", "") == "text")


def _parse_json(text: str) -> dict:
    """Extract the first JSON object from model text."""
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError(f"no JSON object in judge output: {text[:200]!r}")
    return json.loads(m.group(0))


# ── Eval 2: rubric judge ─────────────────────────────────────────────────────
def rubric_judge(
    output: str,
    rubric: str,
    model: str = JUDGE_MODEL,
    classify_fn: Callable[[str, str], str] | None = None,
) -> EvaluationResult:
    classify = classify_fn or _anthropic_classify
    prompt = RUBRIC_TEMPLATE.format(output=output, rubric=rubric)
    raw = classify(prompt, model)
    try:
        parsed = _parse_json(raw)
        label = str(parsed.get("label", "")).strip().lower()
        explanation = str(parsed.get("explanation", "")).strip()
    except Exception:  # noqa: BLE001 — degrade gracefully to rail search
        label = "good" if re.search(r"\bgood\b", raw, re.I) else "bad"
        explanation = raw.strip()[:300]
    if label not in ("good", "bad"):
        label = "bad"
    return EvaluationResult(
        label=label,
        score=1.0 if label == "good" else 0.0,
        explanation=explanation,
        metadata={"gate": "eval2_functional", "path": "rubric", "model": model},
    )


# ── Eval 3: pairwise blinded judge ───────────────────────────────────────────
def pairwise_judge(
    task: str,
    output_a: str,
    output_b: str,
    model: str = JUDGE_MODEL,
    classify_fn: Callable[[str, str], str] | None = None,
) -> dict:
    """Returns {winner, score_a, score_b, rationale}. Inputs must already be
    stripped of variant-identifying content by the caller (Eval 3)."""
    classify = classify_fn or _anthropic_classify
    prompt = PAIRWISE_TEMPLATE.format(task=task, output_a=output_a, output_b=output_b)
    raw = classify(prompt, model)
    try:
        parsed = _parse_json(raw)
        winner = str(parsed.get("winner", "tie")).strip().upper()
        winner = winner if winner in ("A", "B", "TIE") else "TIE"
        return {
            "winner": winner,
            "score_a": float(parsed.get("score_a", 0)),
            "score_b": float(parsed.get("score_b", 0)),
            "rationale": str(parsed.get("rationale", "")).strip(),
        }
    except Exception:  # noqa: BLE001
        return {"winner": "TIE", "score_a": 0.0, "score_b": 0.0, "rationale": raw.strip()[:300]}
