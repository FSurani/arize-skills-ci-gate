"""Structural test for the Eval Hub evaluator definitions (offline, no Arize).

Guards the contract ci/gate.py + setup.py rely on: every skill's evaluators are
defined, mapped, and reference real columns.
"""
import eval_hub as hub


def test_every_skill_eval_is_defined_and_mapped():
    for skill, logicals in hub.SKILL_EVALS.items():
        assert logicals, f"{skill} has no evaluators"
        for logical in logicals:
            assert logical in hub.EVALUATORS, f"{skill} references unknown evaluator {logical}"
            assert logical in hub.MAPPINGS, f"{logical} has no column mapping"
            spec = hub.EVALUATORS[logical]
            assert spec["kind"] in ("code", "template")
            assert spec["name"] and "eval" not in spec["name"].split("-")[0]  # descriptive, not "evalN"
            assert spec["col"]


def test_each_skill_has_exactly_one_functional_path():
    # verifiable -> verifier, non-verifiable -> rubric; never both / neither
    for skill, logicals in hub.SKILL_EVALS.items():
        functional = [l for l in logicals if l in ("verifier", "rubric")]
        assert len(functional) == 1, f"{skill} should have exactly one functional eval, got {functional}"


def test_code_evaluators_are_self_contained_strings():
    for logical, spec in hub.EVALUATORS.items():
        if spec["kind"] == "code":
            assert "CodeEvaluator" in spec["code"]
            assert "return EvaluationResult" in spec["code"]
        else:
            assert "{output}" in spec["template"] and "{rubric}" in spec["template"]
