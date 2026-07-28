"""Eval 1 unit test: synthetic triggered/untriggered traces
produce the correct FP/FN rates, per arm."""
from common import RunResult, ToolSpan, skill_marker_path
import eval1_trigger as e1

SKILL = "api-helper"
MARKER = skill_marker_path(SKILL)  # .claude/skills/api-helper/SKILL.md


def _run(case_id, arm, triggered):
    spans = []
    if triggered:
        spans.append(ToolSpan(name="Read", input={"file_path": f"/sandbox/{MARKER}"}))
    spans.append(ToolSpan(name="Bash", input={"command": "python do_task.py"}))
    return RunResult(case_id=case_id, skill=SKILL, arm=arm, tool_spans=spans)


CASES = [
    {"id": "t01", "should_trigger": True},
    {"id": "t02", "should_trigger": True},
    {"id": "n01", "should_trigger": False},
    {"id": "n02", "should_trigger": False},
]


def test_variant_a_false_positives_on_negatives():
    # variant_a: triggers on everything (broad description) → FP on both negatives
    runs = [_run(c["id"], "variant_a", True) for c in CASES]
    m = e1.metrics_by_arm(CASES, runs, SKILL)["variant_a"]
    assert m["fp_rate"] == 1.0
    assert m["fn_rate"] == 0.0
    assert set(m["false_positives"]) == {"n01", "n02"}


def test_variant_b_clean():
    # variant_b: triggers only on positives (tight description) → no FP, no FN
    runs = [_run(c["id"], "variant_b", c["should_trigger"]) for c in CASES]
    m = e1.metrics_by_arm(CASES, runs, SKILL)["variant_b"]
    assert m["fp_rate"] == 0.0
    assert m["fn_rate"] == 0.0


def test_detection_uses_shared_marker():
    # a positive that failed to load the skill is a false negative
    runs = [_run("t01", "variant_b", False)]
    cases = [{"id": "t01", "should_trigger": True}]
    m = e1.metrics_by_arm(cases, runs, SKILL)["variant_b"]
    assert m["fn_rate"] == 1.0
    assert m["false_negatives"] == ["t01"]


def test_detects_real_skill_tooluse():
    # real Claude Code loads a skill via a Skill tool-use {"skill": "<name>"}
    from common import RunResult, ToolSpan, detect_triggered
    spans = [ToolSpan(name="ToolSearch", input={"query": "orders"}),
             ToolSpan(name="Skill", input={"skill": "api-helper"}),
             ToolSpan(name="Bash", input={"command": "curl ..."})]
    assert detect_triggered(spans, "api-helper") is True
    assert detect_triggered(spans, "story-writer") is False  # different skill
    # no Skill tool-use → not triggered
    assert detect_triggered([ToolSpan(name="Bash", input={"command": "ls"})], "api-helper") is False


def test_skill_off_excluded():
    runs = [_run("t01", "skill_off", False)]
    cases = [{"id": "t01", "should_trigger": True}]
    assert e1.metrics_by_arm(cases, runs, SKILL) == {}
