"""Eval 0 unit test (acceptance §11): the known-bad fixture skill must FAIL, and
the real skills must PASS."""
from pathlib import Path

import eval0_structural as e0

REPO = Path(__file__).resolve().parents[1]
SKILLS = REPO / "skills"


def test_bad_fixture_fails_with_security_findings():
    res = e0.evaluate_skill(SKILLS / "_bad_fixture")
    assert not res.passed
    failed = {c["name"] for c in res.metadata["checks"] if not c["passed"]}
    # both a structural failure and a security failure must be present
    assert "frontmatter_parses" in failed
    assert "no_prompt_injection" in failed
    assert "no_embedded_secrets" in failed
    assert "bundled_scripts_safe" in failed


def test_injection_finding_is_reported():
    res = e0.evaluate_skill(SKILLS / "_bad_fixture")
    inj = next(c for c in res.metadata["checks"] if c["name"] == "no_prompt_injection")
    assert not inj["passed"]
    assert any("override" in f or "exfiltra" in f or "hide" in f for f in inj["findings"])


def test_real_skills_pass():
    for rel in ("api-helper/variant_a", "api-helper/variant_b", "story-writer"):
        res = e0.evaluate_skill(SKILLS / rel)
        assert res.passed, f"{rel} unexpectedly failed: {res.explanation}"


def test_legit_own_auth_token_not_flagged():
    # variant_b references ORDERS_API_TOKEN (its own auth) — must NOT trip injection
    res = e0.evaluate_skill(SKILLS / "api-helper/variant_b")
    inj = next(c for c in res.metadata["checks"] if c["name"] == "no_prompt_injection")
    assert inj["passed"]
