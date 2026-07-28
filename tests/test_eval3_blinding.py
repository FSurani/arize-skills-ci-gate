"""Eval 3 blinding unit test: the text handed to the judge must
contain NO variant-identifying strings."""
import re

from common import RunResult
import eval3_pairwise as e3


def _fake_classify(prompt, model):
    # deterministic stand-in for the LLM — also lets us capture the prompt
    _fake_classify.last_prompt = prompt
    return '{"winner": "B", "score_a": 3, "score_b": 5, "rationale": "B is clearer."}'


CASE = {"id": "t01", "prompt": "Count all orders and write the total to count.txt."}


def test_judge_input_has_no_variant_markers():
    # plant leak strings in both outputs; sanitizer must scrub them
    run_a = RunResult(case_id="t01", skill="api-helper", arm="variant_a",
                      final_output="I loaded variant_a from .claude/skills/api-helper/SKILL.md and got 5.")
    run_b = RunResult(case_id="t01", skill="api-helper", arm="variant_b",
                      final_output="Using variant_b, the total is 5.")
    out = e3.judge_case(CASE, run_a, run_b, classify_fn=_fake_classify)

    blob = out["blinded_inputs"] + "\n" + _fake_classify.last_prompt
    assert not re.search(r"variant[_\s-]?[ab]", blob, re.I), "variant identity leaked to judge"
    assert ".claude/skills/" not in blob, "skill path leaked to judge"


def test_winner_maps_back_through_swap():
    run_a = RunResult(case_id="t01", skill="api-helper", arm="variant_a", final_output="answer: 5")
    run_b = RunResult(case_id="t01", skill="api-helper", arm="variant_b", final_output="answer: 5")
    out = e3.judge_case(CASE, run_a, run_b, classify_fn=_fake_classify)
    # fake says winner "B" (the second slot); mapping must resolve to a real variant
    assert out["winner_variant"] in ("variant_a", "variant_b")
    # variant_b should carry the score the judge gave to whichever slot it occupied
    assert out["score_variant_a"] in (3.0, 5.0) and out["score_variant_b"] in (3.0, 5.0)


def test_order_is_deterministic():
    assert e3._order_for("t01") == e3._order_for("t01")
