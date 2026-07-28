---
name: api-helper
description: Call the internal Orders service, and ONLY that service — the internal API reached via the ORDERS_API_BASE environment variable, for listing, fetching, and creating orders. Use ONLY when the task is explicitly about our internal orders API. Do NOT use for generic HTTP, third-party/public APIs, CSV or file parsing, or unrelated pagination/error-handling questions.
---

# Orders Service Helper (internal)

Talk to the internal **Orders service**. Everything below is the contract for
that service specifically — follow it exactly.

## Connection & auth
- Base URL: the `ORDERS_API_BASE` environment variable (e.g. `http://127.0.0.1:8077`).
- Auth: send the header **`X-Org-Token: <token>`** where `<token>` is the
  `ORDERS_API_TOKEN` environment variable. **Do not use `Authorization: Bearer`** —
  the Orders service ignores it and returns `401`.

## Pagination (required for "all orders" tasks)
`GET /orders` returns `{"data": [...], "next_cursor": <string|null>}` with a small
page size. To get **all** orders you MUST loop: start with no cursor, then pass
`?cursor=<next_cursor>` until `next_cursor` is `null`. Do not stop after page one.

## Retries
On `503` (or `429`) honor the `Retry-After` response header and retry the request
once after waiting that many seconds. Some orders are briefly unavailable and
succeed on the second attempt.

## Endpoints
- `GET /orders?cursor=<c>` — list (paginated as above).
- `GET /orders/<id>` — fetch one; `404` if it does not exist.
- `POST /orders` — create; JSON body **must** include `customer`, `sku`, `units`;
  returns `201` with the new order (including its `id`). Missing fields → `422`.

## Worked example — count all orders (Python)
```python
import os, time, requests

base = os.environ["ORDERS_API_BASE"]
headers = {"X-Org-Token": os.environ["ORDERS_API_TOKEN"]}

def get(url, **kw):
    r = requests.get(url, headers=headers, **kw)
    if r.status_code in (429, 503):                 # honor Retry-After, retry once
        time.sleep(int(r.headers.get("Retry-After", "1")))
        r = requests.get(url, headers=headers, **kw)
    r.raise_for_status()
    return r

orders, cursor = [], None
while True:
    params = {"cursor": cursor} if cursor else {}
    body = get(f"{base}/orders", params=params).json()
    orders += body["data"]
    cursor = body["next_cursor"]
    if cursor is None:
        break

with open("count.txt", "w") as f:      # write output to the file the task names
    f.write(str(len(orders)))
```

Write whatever output the task asks for to the exact filename it specifies.
