"""Deterministic verifiers for Skill A (api-helper) task cases.

Each `verifiers/api_helper/<case_id>.py` is a thin shim that calls
`make_verify("<case_id>")` from here, so the per-case entry points exist while
the assertions live in one testable place.

A verifier receives the final sandbox state and passes/fails on TWO kinds of
evidence:
  1. the OUTPUT the agent wrote to the file the task named, and
  2. HOW it called the mock Orders API — read from the request log the mock
     writes to `orders_api_log.jsonl` (auth header used, pagination followed,
     retry on 503, POST validation), so we catch skills that guess the answer
     or use the wrong auth convention.

Emits the shared `EvaluationResult` (label/score/explanation) so the verifier
path and the rubric path are indistinguishable downstream.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "gates"))
from common import EvaluationResult  # noqa: E402

LOG_FILENAME = "orders_api_log.jsonl"
FLAKY_ID = "ord_0003"


# ── sandbox / log readers ────────────────────────────────────────────────────
def read_output(workspace: Path, filename: str) -> str | None:
    p = Path(workspace) / filename
    return p.read_text().strip() if p.exists() else None


def read_log(workspace: Path) -> list[dict]:
    p = Path(workspace) / LOG_FILENAME
    if not p.exists():
        return []
    out = []
    for line in p.read_text().splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


# ── request-log predicates ───────────────────────────────────────────────────
def used_org_token(log: list[dict]) -> bool:
    return any("X-Org-Token" in (r.get("headers") or {}) for r in log)


def used_bearer(log: list[dict]) -> bool:
    return any("Bearer" in (r.get("headers", {}) or {}).get("Authorization", "") for r in log)


def any_ok(log: list[dict]) -> bool:
    return any(r.get("status") in (200, 201) for r in log)


def n_list_pages(log: list[dict]) -> int:
    starts = {r.get("page_start") for r in log
              if r.get("path") == "/orders" and r.get("status") == 200}
    starts.discard(None)
    return len(starts)


def retried_flaky(log: list[dict], oid: str = FLAKY_ID) -> bool:
    seq = [r.get("status") for r in log if r.get("path") == f"/orders/{oid}"]
    return 503 in seq and any(s == 200 and i > seq.index(503) for i, s in enumerate(seq))


def got_ok(log: list[dict], oid: str) -> bool:
    return any(r.get("path") == f"/orders/{oid}" and r.get("status") == 200 for r in log)


def got_404(log: list[dict], oid: str) -> bool:
    return any(r.get("path") == f"/orders/{oid}" and r.get("status") == 404 for r in log)


def posts(log: list[dict]) -> list[dict]:
    return [r for r in log if r.get("method") == "POST" and r.get("path") == "/orders"]


# ── value matching (tolerant but deterministic) ──────────────────────────────
def match_number(text: str | None, expected: int) -> bool:
    if text is None:
        return False
    if text.strip() == str(expected):
        return True
    nums = re.findall(r"-?\d+", text)
    # tolerate a short sentence, but only if exactly one number appears and it matches
    return len(nums) == 1 and int(nums[0]) == expected


def match_str(text: str | None, expected: str) -> bool:
    if text is None:
        return False
    t = text.strip()
    return t.lower() == expected.lower() or expected.lower() in t.lower()


# ── result helper ────────────────────────────────────────────────────────────
def _result(ok: bool, ref: str, detail: str, evidence: dict) -> EvaluationResult:
    return EvaluationResult(
        label="pass" if ok else "fail",
        score=1.0 if ok else 0.0,
        explanation=detail,
        metadata={"gate": "eval2_functional", "path": "verifier", "ref": ref, "evidence": evidence},
    )


# ─────────────────────────────────────────────────────────────────────────────
# Per-case verifier specs. Each takes (workspace: Path, log) and returns bool +
# a human detail string + evidence dict.
# Expected answers are derived from mockapi/orders_api.py with ORDERS_SEED=5.
# ─────────────────────────────────────────────────────────────────────────────
def _v_count(ws, log):  # t01
    got = read_output(ws, "count.txt")
    ok_val = match_number(got, 5)
    ok = ok_val and used_org_token(log) and any_ok(log)
    return ok, f"count.txt={got!r} (want 5); org-token={used_org_token(log)}; pages={n_list_pages(log)}", \
        {"output": got, "auth_ok": used_org_token(log), "pages": n_list_pages(log), "used_bearer": used_bearer(log)}


def _v_customer(ws, log):  # t02
    got = read_output(ws, "customer.txt")
    ok = match_str(got, "Globex") and got_ok(log, "ord_0001")
    return ok, f"customer.txt={got!r} (want Globex); got200(ord_0001)={got_ok(log,'ord_0001')}", \
        {"output": got, "auth_ok": used_org_token(log)}


def _v_create(ws, log):  # t03
    got = read_output(ws, "new_id.txt")
    good_posts = [p for p in posts(log) if p.get("status") == 201
                  and (p.get("json") or {}).get("customer") == "ACME"
                  and (p.get("json") or {}).get("sku") == "WIDGET-1"
                  and int((p.get("json") or {}).get("units", -1)) == 2]
    created_ids = [p.get("created") for p in good_posts]
    ok = bool(good_posts) and got is not None and any(match_str(got, cid) for cid in created_ids if cid)
    return ok, f"new_id.txt={got!r}; valid 201 POST(ACME/WIDGET-1/2)={bool(good_posts)}; created={created_ids}", \
        {"output": got, "created_ids": created_ids}


def _v_flaky_status(ws, log):  # t04
    got = read_output(ws, "status.txt")
    ok = match_str(got, "shipped") and got_ok(log, FLAKY_ID)
    return ok, f"status.txt={got!r} (want shipped); retried={retried_flaky(log)}; got200={got_ok(log,FLAKY_ID)}", \
        {"output": got, "retried": retried_flaky(log)}


def _v_open_count(ws, log):  # t05
    got = read_output(ws, "open_count.txt")
    ok = match_number(got, 3) and used_org_token(log) and any_ok(log)
    return ok, f"open_count.txt={got!r} (want 3); pages={n_list_pages(log)}", \
        {"output": got, "pages": n_list_pages(log)}


def _v_sku(ws, log):  # t06
    got = read_output(ws, "sku.txt")
    ok = match_str(got, "WIDGET-3") and got_ok(log, "ord_0002")
    return ok, f"sku.txt={got!r} (want WIDGET-3)", {"output": got}


def _v_ids(ws, log):  # t07
    got = read_output(ws, "ids.txt")
    lines = [l.strip() for l in (got or "").splitlines() if l.strip()]
    expected = [f"ord_{i:04d}" for i in range(5)]
    ok = lines == expected and used_org_token(log)
    return ok, f"ids.txt lines={lines} (want {expected}); pages={n_list_pages(log)}", \
        {"output": lines, "pages": n_list_pages(log)}


def _v_created_status(ws, log):  # t08
    got = read_output(ws, "created_status.txt")
    good = [p for p in posts(log) if p.get("status") == 201
            and (p.get("json") or {}).get("customer") == "Globex"
            and (p.get("json") or {}).get("sku") == "WIDGET-2"
            and int((p.get("json") or {}).get("units", -1)) == 5]
    ok = match_str(got, "open") and bool(good)
    return ok, f"created_status.txt={got!r} (want open); valid POST={bool(good)}", {"output": got}


def _v_max_order(ws, log):  # t12
    got = read_output(ws, "max_order.txt")
    ok = match_str(got, "ord_0003") and used_org_token(log)
    return ok, f"max_order.txt={got!r} (want ord_0003); pages={n_list_pages(log)}", \
        {"output": got, "pages": n_list_pages(log)}


def _v_missing_units(ws, log):  # e02
    got = read_output(ws, "status_code.txt")
    bad_posts = [p for p in posts(log) if p.get("status") == 422
                 and "units" not in (p.get("json") or {})]
    ok = match_number(got, 422) and bool(bad_posts)
    return ok, f"status_code.txt={got!r} (want 422); 422-POST-without-units={bool(bad_posts)}", \
        {"output": got, "did_not_invent_units": bool(bad_posts)}


def _v_acme(ws, log):  # t09 (holdout)
    got = read_output(ws, "acme.txt")
    ok = match_number(got, 1) and used_org_token(log) and any_ok(log)
    return ok, f"acme.txt={got!r} (want 1)", {"output": got, "pages": n_list_pages(log)}


def _v_total_units(ws, log):  # t10 (holdout)
    got = read_output(ws, "total_units.txt")
    ok = match_number(got, 11) and used_org_token(log) and any_ok(log)
    return ok, f"total_units.txt={got!r} (want 11); pages={n_list_pages(log)}", \
        {"output": got, "pages": n_list_pages(log)}


def _v_not_found(ws, log):  # e01 (holdout)
    got = read_output(ws, "out.txt")
    ok = match_str(got, "NOT_FOUND") and got_404(log, "ord_9999")
    return ok, f"out.txt={got!r} (want NOT_FOUND); got404={got_404(log,'ord_9999')}", {"output": got}


_SPECS = {
    "t01": _v_count, "t02": _v_customer, "t03": _v_create, "t04": _v_flaky_status,
    "t05": _v_open_count, "t06": _v_sku, "t07": _v_ids, "t08": _v_created_status,
    "t12": _v_max_order, "e02": _v_missing_units,
    "t09": _v_acme, "t10": _v_total_units, "e01": _v_not_found,
}


def make_verify(ref: str):
    fn = _SPECS[ref]

    def verify(workspace, log=None) -> EvaluationResult:
        ws = Path(workspace)
        log = read_log(ws) if log is None else log
        ok, detail, evidence = fn(ws, log)
        return _result(ok, ref, detail, evidence)

    return verify
