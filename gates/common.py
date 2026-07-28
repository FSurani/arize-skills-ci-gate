"""Shared, harness-agnostic types for all gates (Evals 0-4).

Design rule (plan §12): NOTHING in `gates/` may import Claude-Code / Agent-SDK
symbols. Gates consume a normalized `RunResult` (produced by whatever harness ran
the case) plus, for verifiers, the final sandbox state. This lets a second harness
(Cursor, OpenHands, ...) be added later with zero evaluator changes.

Every gate emits the SAME `EvaluationResult` shape (label / score / explanation)
regardless of whether it ran a deterministic verifier or an LLM rubric judge, so
the CI gate and the report never branch on "which path ran" (plan §5, Eval 2).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any


# ─────────────────────────────────────────────────────────────────────────────
# The one eval schema, shared by every gate + both Eval-2 paths.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class EvaluationResult:
    """Uniform result for any single evaluation.

    score:      1.0 = pass, 0.0 = fail (continuous allowed for pairwise 1-5 → /5).
    label:      short rail, e.g. "pass"/"fail", "good"/"bad", "a"/"b".
    explanation: free text — load-bearing (feeds report + demo beat 4).
    metadata:   gate-specific extras (fp/fn counts, tokens, winner, ...).
    """

    label: str
    score: float
    explanation: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def passed(self) -> bool:
        return self.score >= 1.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ─────────────────────────────────────────────────────────────────────────────
# Normalized run record — the contract between harness and gates.
#
# harness/run_case.py writes one of these per (case, skill, arm, trial) as
# result.json. Gates read THIS, never the AX API and never harness internals.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class ToolSpan:
    name: str                      # e.g. "Read", "Bash", "Edit"
    input: dict[str, Any] = field(default_factory=dict)
    output: str = ""
    duration_ms: float = 0.0


@dataclass
class RunResult:
    case_id: str
    skill: str
    arm: str                       # variant_a | variant_b | v1 | skill_off
    trial: int = 0

    # tracing linkage (AX system-of-record)
    session_id: str = ""
    trace_id: str = ""

    # normalized signals extracted from the trace / SDK stream
    tool_spans: list[ToolSpan] = field(default_factory=list)
    final_output: str = ""
    turns: int = 0
    tokens_input: int = 0          # fresh input (uncached + cache-creation), gated by Eval 4
    tokens_output: int = 0
    tokens_cache_read: int = 0      # cache-read replay — recorded for transparency, NOT gated
    wall_clock_s: float = 0.0
    est_cost_usd: float = 0.0

    # filesystem linkage (verifier path reads final sandbox state)
    workspace_dir: str = ""
    transcript_path: str = ""

    @property
    def tokens_total(self) -> int:
        return self.tokens_input + self.tokens_output

    # ── (de)serialization ────────────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunResult":
        spans = [ToolSpan(**s) for s in d.get("tool_spans", [])]
        known = {f for f in cls.__dataclass_fields__}  # noqa: C416
        kwargs = {k: v for k, v in d.items() if k in known and k != "tool_spans"}
        return cls(tool_spans=spans, **kwargs)

    @classmethod
    def load(cls, path: str | Path) -> "RunResult":
        return cls.from_dict(json.loads(Path(path).read_text()))

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))


# ─────────────────────────────────────────────────────────────────────────────
# Skill-load detection — shared by the harness (to stamp `triggered`) and Eval 1.
# Kept here so the detection logic has exactly one definition and can be
# unit-tested against synthetic trace fixtures (plan §4 step 3, acceptance §11).
# ─────────────────────────────────────────────────────────────────────────────
def skill_marker_path(skill_name: str) -> str:
    """The path substring that indicates the skill's SKILL.md was loaded."""
    return f".claude/skills/{skill_name}/SKILL.md"


def detect_triggered(tool_spans: list[ToolSpan], skill_name: str) -> bool:
    """True iff the skill was loaded during the run.

    Verified on a real Claude Code trace (2026-07-27): a skill loads via a
    **`Skill` tool-use** whose input is `{"skill": "<name>"}` — NOT a Read of
    SKILL.md. We detect that primarily, and keep an explicit-SKILL.md-path check
    as a fallback (mock traces / other harnesses / Grep-Glob access).
    """
    marker = skill_marker_path(skill_name)
    for span in tool_spans:
        # primary: the real Claude Code skill-load signal
        if span.name == "Skill":
            sv = span.input.get("skill") or span.input.get("name")
            if isinstance(sv, str) and sv == skill_name:
                return True
        # fallback: an explicit reference to the skill's SKILL.md path
        for v in span.input.values():
            if isinstance(v, str) and marker in v:
                return True
            if isinstance(v, (list, tuple)):
                if any(isinstance(x, str) and marker in x for x in v):
                    return True
    return False


def load_cases(path: str | Path) -> list[dict[str, Any]]:
    """Load a JSONL dataset file into a list of case dicts."""
    out: list[dict[str, Any]] = []
    for line in Path(path).read_text().splitlines():
        line = line.strip()
        if line:
            out.append(json.loads(line))
    return out
