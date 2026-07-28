"""Local mock of the internal Orders service.

Runs inside the sandbox during a harness run. It exists so verifiers (Eval 2,
verifier path) can assert on *how the agent called the API* — not just the final
answer. Every request is appended to a JSON-lines request log at $ORDERS_API_LOG,
which verifiers read to check auth header, pagination follow-through, and retry
behavior.

The contract encodes the three things the api-helper SKILL.md teaches — and the
three ways a naive agent gets it wrong:

  1. AUTH  — custom header `X-Org-Token: <token>`, NOT `Authorization: Bearer`.
             Wrong/missing token → 401. (Naive agents reach for Bearer.)
  2. PAGE  — GET /orders returns {"data":[...], "next_cursor": <str|null>} with a
             SMALL page size, so all-orders tasks require following next_cursor
             until null. (Naive agents grab page 1 and stop.)
  3. RETRY — one designated order id is "flaky": the first GET returns 503 with a
             Retry-After header, then succeeds. (Naive agents don't retry.)

Config via env:
  ORDERS_API_TOKEN   valid token (default "demo-secret-token")
  ORDERS_API_LOG     path to the request log (default ./orders_api_log.jsonl)
  ORDERS_SEED        integer; controls how many orders exist (default 5)
  PORT               port to bind (default 8077)
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from flask import Flask, jsonify, request

app = Flask(__name__)

TOKEN = os.environ.get("ORDERS_API_TOKEN", "demo-secret-token")
LOG_PATH = Path(os.environ.get("ORDERS_API_LOG", "orders_api_log.jsonl"))
N_ORDERS = int(os.environ.get("ORDERS_SEED", "5"))
PAGE_SIZE = 2                      # small on purpose → pagination required
FLAKY_ORDER_ID = "ord_0003"        # first GET 503s, then succeeds

# in-memory store ─────────────────────────────────────────────────────────────
_ORDERS: dict[str, dict] = {
    f"ord_{i:04d}": {
        "id": f"ord_{i:04d}",
        "customer": ["ACME", "Globex", "Initech", "Umbrella", "Stark"][i % 5],
        "sku": f"WIDGET-{i % 3 + 1}",
        "units": (i % 4) + 1,
        "status": "open" if i % 2 == 0 else "shipped",
    }
    for i in range(N_ORDERS)
}
_flaky_hits: dict[str, int] = {}   # order_id -> times seen (drives 503-then-200)


def _log(status: int, extra: dict | None = None) -> None:
    rec = {
        "method": request.method,
        "path": request.path,
        "query": dict(request.args),
        "headers": {k: v for k, v in request.headers.items()},
        "json": request.get_json(silent=True),
        "status": status,
    }
    if extra:
        rec.update(extra)
    with LOG_PATH.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


def _authed() -> bool:
    return request.headers.get("X-Org-Token") == TOKEN


@app.get("/orders")
def list_orders():
    if not _authed():
        _log(401)
        return jsonify({"error": "missing or invalid X-Org-Token"}), 401
    ids = sorted(_ORDERS)
    cursor = request.args.get("cursor")
    start = int(cursor) if cursor and cursor.isdigit() else 0
    page = ids[start:start + PAGE_SIZE]
    nxt = start + PAGE_SIZE
    next_cursor = str(nxt) if nxt < len(ids) else None
    _log(200, {"page_start": start})
    return jsonify({"data": [_ORDERS[i] for i in page], "next_cursor": next_cursor})


@app.get("/orders/<order_id>")
def get_order(order_id: str):
    if not _authed():
        _log(401)
        return jsonify({"error": "missing or invalid X-Org-Token"}), 401
    # flaky order: 503 on first contact, then 200
    if order_id == FLAKY_ORDER_ID:
        _flaky_hits[order_id] = _flaky_hits.get(order_id, 0) + 1
        if _flaky_hits[order_id] == 1:
            _log(503)
            resp = jsonify({"error": "temporarily unavailable"})
            resp.headers["Retry-After"] = "1"
            return resp, 503
    if order_id not in _ORDERS:
        _log(404)
        return jsonify({"error": "not found"}), 404
    _log(200)
    return jsonify(_ORDERS[order_id])


@app.post("/orders")
def create_order():
    if not _authed():
        _log(401)
        return jsonify({"error": "missing or invalid X-Org-Token"}), 401
    body = request.get_json(silent=True) or {}
    required = {"customer", "sku", "units"}
    missing = required - set(body)
    if missing:
        _log(422, {"missing": sorted(missing)})
        return jsonify({"error": f"missing fields: {sorted(missing)}"}), 422
    new_id = f"ord_{len(_ORDERS):04d}"
    _ORDERS[new_id] = {"id": new_id, "status": "open", **{k: body[k] for k in required}}
    _log(201, {"created": new_id})
    return jsonify(_ORDERS[new_id]), 201


@app.get("/healthz")
def healthz():
    return jsonify({"ok": True, "orders": len(_ORDERS)})


if __name__ == "__main__":
    # truncate the log on boot so each run starts clean
    LOG_PATH.write_text("")
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "8077")))
