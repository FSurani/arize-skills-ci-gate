"""Eval 2 unit test (acceptance §11): the verifier path and the rubric path emit
the SAME EvaluationResult schema, so downstream gating is path-agnostic."""
import json
from pathlib import Path

from common import RunResult
import eval2_functional as e2


def _keys(res):
    return set(res.to_dict().keys())


def test_verifier_and_rubric_same_schema(tmp_path):
    # ── verifier path (t01) ──
    ws = tmp_path / "ws"
    ws.mkdir()
    (ws / "count.txt").write_text("5")
    log = [{"method": "GET", "path": "/orders", "headers": {"X-Org-Token": "t"},
            "status": 200, "page_start": 0}]
    (ws / "orders_api_log.jsonl").write_text("\n".join(json.dumps(r) for r in log))
    run_v = RunResult(case_id="t01", skill="api-helper", arm="variant_b",
                      workspace_dir=str(ws))
    case_v = {"id": "t01", "check": {"type": "verifier", "ref": "t01"}}
    res_v = e2.evaluate(case_v, run_v)

    # ── rubric path (fake judge, no tokens spent) ──
    run_r = RunResult(case_id="s01", skill="story-writer", arm="v1",
                      final_output="As a returning customer, I want ...")
    case_r = {"id": "s01", "check": {"type": "rubric", "rubric": "must be INVEST"}}
    res_r = e2.evaluate(case_r, run_r,
                        classify_fn=lambda p, m: '{"label":"good","explanation":"ok"}')

    assert _keys(res_v) == _keys(res_r) == {"label", "score", "explanation", "metadata"}
    for res in (res_v, res_r):
        assert res.label in ("pass", "fail", "good", "bad")
        assert res.score in (0.0, 1.0)


def test_negative_case_skipped():
    run = RunResult(case_id="n01", skill="api-helper", arm="variant_a")
    assert e2.evaluate({"id": "n01", "check": None}, run) is None
