"""AX experiment layer — the system-of-record for skill versions (plan §6).

CLI-first (the team's stated goal): uses the `ax` CLI for datasets + experiments.
One experiment per (skill, arm), named by SKILL.md **content hash** so naming is
append-only and never needs a delete (plan §6). Evals 1-4 computed locally are
attached as pass-through columns on the experiment runs, so AX shows populated
eval columns (acceptance §11).

Live-validated recipe (ax v0.25, 2026-07-27) — and the pre-release quirks it
works around:
  1. `ax datasets create -n NAME -s <BASE64 space GID> -f FILE`
     · the space MUST be the base64 GID form (the AX-console value); the decoded
       'Space:N:x' form is rejected as "space not found" by create.
     · example rows may NOT contain a column named `id` (platform-managed) — we
       carry our case id as `case_id`.
     · create returns the dataset **id**; we address everything after by id.
  2. `ax datasets export <DATASET_ID> --stdout`  (BY ID — no -s; name+space
     resolution and `datasets list` are unreliable for this space, so we persist
     the returned id locally for idempotency instead of re-looking-it-up).
     export rows are {id: <example UUID>, additional_properties: {case_id, ...}}.
  3. `ax experiments create -n NAME --dataset <DATASET_ID> -s <BASE64> -f RUNS`
     · RUNS needs `example_id` (the export UUID) + `output`; extras pass through.

Degrades gracefully: every AX call is best-effort. If creds/CLI are missing the
functions log and return None, so the LOCAL gate (ci/gate.py) is never blocked.
AX-side template / Agent-as-a-Judge evaluators additionally need an AI
integration id (ARIZE_AI_INTEGRATION_ID) — scaffolded but optional.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "gates"))
from common import RunResult  # noqa: E402

# ARIZE_SPACE_ID stays in its console/base64/tracing form; the CLI write commands
# (create) want exactly this form.
SPACE = os.environ.get("ARIZE_SPACE_ID", "")
PROFILE = os.environ.get("ARIZE_PROFILE", "gic-ci")
STATE_PATH = REPO / "report" / "out" / "ax_state.json"

SKILL_SRC = {
    ("api-helper", "variant_a"): REPO / "skills/api-helper/variant_a/SKILL.md",
    ("api-helper", "variant_b"): REPO / "skills/api-helper/variant_b/SKILL.md",
    ("story-writer", "v1"): REPO / "skills/story-writer/SKILL.md",
}

PRINT_ONLY = os.environ.get("AX_PRINT_ONLY") == "1"
if PRINT_ONLY and not SPACE:
    SPACE = "<ARIZE_SPACE_ID>"


# ─────────────────────────────────────────────────────────────────────────────
# Naming (content-hash, append-only)
# ─────────────────────────────────────────────────────────────────────────────
def content_hash(skill: str, arm: str) -> str:
    if arm == "skill_off":
        return "baseline-off"
    return hashlib.sha256(SKILL_SRC[(skill, arm)].read_bytes()).hexdigest()[:8]


def experiment_name(skill: str, arm: str) -> str:
    return f"{skill}@{content_hash(skill, arm)}"


def dataset_name(skill: str) -> str:
    return f"{skill}-cases"


# ─────────────────────────────────────────────────────────────────────────────
# ax CLI helpers
# ─────────────────────────────────────────────────────────────────────────────
def _mask(args: tuple[str, ...]) -> list[str]:
    out, redact = [], False
    for a in args:
        out.append("<ARIZE_API_KEY>" if redact else a)
        redact = (a == "--api-key")
    return out


def _ax(*args: str) -> subprocess.CompletedProcess | None:
    if PRINT_ONLY:
        print("  $ ax " + " ".join(_mask(args)))
        return subprocess.CompletedProcess(args, 0, "id: <ID>\n", "")
    try:
        return subprocess.run(["ax", *args], capture_output=True, text=True, timeout=300)
    except FileNotFoundError:
        print("[ax] CLI not found — skipping AX step", file=sys.stderr)
        return None


def _field(stdout: str, name: str) -> str | None:
    """Pull `name: VALUE` out of the CLI's table output (create/get return this)."""
    m = re.search(rf"\b{name}:\s*([^\s│]+)", stdout or "")
    return m.group(1) if m else None


def _load_state() -> dict:
    if STATE_PATH.exists():
        try:
            return json.loads(STATE_PATH.read_text())
        except json.JSONDecodeError:
            return {}
    return {}


def _save_state(state: dict) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, indent=2))


def ax_available() -> bool:
    if PRINT_ONLY:
        return True
    return bool(SPACE and os.environ.get("ARIZE_API_KEY")) and _ax("--version") is not None


def ensure_profile() -> bool:
    """Create the CI profile non-interactively; 'already exists' counts as success."""
    if not PRINT_ONLY and not os.environ.get("ARIZE_API_KEY"):
        return False
    api_key = os.environ.get("ARIZE_API_KEY", "<ARIZE_API_KEY>")
    res = _ax("profiles", "create", PROFILE, "--api-key", api_key, "--auth-method", "api-key")
    if not res:
        return False
    blob = ((res.stdout or "") + (res.stderr or "")).lower()
    if res.returncode == 0 or "already exists" in blob:
        return True
    print(f"[ax] profile create failed: {blob.strip()[:200]}", file=sys.stderr)
    return False


# ─────────────────────────────────────────────────────────────────────────────
# Datasets (create-by-base64-space, address-by-id, idempotent via local state)
# ─────────────────────────────────────────────────────────────────────────────
def ensure_dataset(skill: str, cases: list[dict]) -> str | None:
    """Return the dataset **id**, creating it if we don't already know it.
    Idempotency uses a local state file because `datasets list` is unreliable for
    this space; we never delete (plan §6)."""
    name = dataset_name(skill)
    state = _load_state()
    if not PRINT_ONLY and state.get("datasets", {}).get(name):
        return state["datasets"][name]

    # examples file — carry `case_id` (NOT `id`, which is platform-managed)
    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        for c in cases:
            fh.write(json.dumps({
                "case_id": c["id"], "prompt": c["prompt"], "class": c.get("class"),
                "should_trigger": c.get("should_trigger"),
                "check": json.dumps(c.get("check")),
            }) + "\n")
        path = fh.name

    create_name = name
    for attempt in range(4):
        res = _ax("datasets", "create", "-n", create_name, "-s", SPACE, "-f", path)
        if not res:
            return None
        blob = (res.stdout or "") + (res.stderr or "")
        if res.returncode == 0:
            ds_id = _field(res.stdout, "id") or "<ID>"
            state.setdefault("datasets", {})[name] = ds_id
            if not PRINT_ONLY:
                _save_state(state)
            print(f"[ax] dataset ready: {create_name} (id={ds_id})")
            return ds_id
        if "already exists" in blob.lower() or "409" in blob:
            create_name = f"{name}-{attempt + 2}"   # name taken but id unknown → new name
            continue
        print(f"[ax] dataset create failed: {blob.strip()[:200]}", file=sys.stderr)
        return None
    return None


def export_example_map(dataset_id: str) -> dict[str, str]:
    """{case_id -> example UUID} via export BY ID (no -s needed)."""
    res = _ax("datasets", "export", dataset_id, "--stdout")
    if not (res and res.returncode == 0):
        return {}
    try:
        rows = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return {}
    out = {}
    for r in rows:
        cid = (r.get("additional_properties") or {}).get("case_id") or r.get("case_id")
        ex_id = r.get("id") or r.get("example_id")
        if cid and ex_id:
            out[cid] = ex_id
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Experiments (create by dataset id + base64 space; append-only names)
# ─────────────────────────────────────────────────────────────────────────────
def create_experiment(skill: str, arm: str, dataset_id: str, run_rows: list[dict]) -> str | None:
    base = experiment_name(skill, arm)
    # idempotent: a content-hash-named experiment we already created = same
    # skill version → don't recreate (avoids -rN clutter on reruns).
    if not PRINT_ONLY and _load_state().get("experiments", {}).get(base):
        return base
    id_map = export_example_map(dataset_id)

    with tempfile.NamedTemporaryFile("w", suffix=".jsonl", delete=False) as fh:
        for row in run_rows:
            ex_id = id_map.get(row["case_id"], row["case_id"])
            fh.write(json.dumps({"example_id": ex_id, **row}) + "\n")
        path = fh.name

    name = base
    for attempt in range(5):
        res = _ax("experiments", "create", "-n", name, "--dataset", dataset_id, "-s", SPACE, "-f", path)
        if not res:
            return None
        blob = (res.stdout or "") + (res.stderr or "")
        if res.returncode == 0:
            # capture the experiment id — export/list by NAME is unreliable, so we
            # persist the id and always read back by id.
            eid = _field(res.stdout, "id")
            if eid and not PRINT_ONLY:
                state = _load_state()
                state.setdefault("experiments", {})[name] = eid
                _save_state(state)
            return name
        if "already exists" in blob.lower() or "409" in blob:
            name = f"{base}-r{attempt + 1}"          # collision → new append-only name
            continue
        print(f"[ax] experiment create failed ({name}): {blob.strip()[:200]}", file=sys.stderr)
        return None
    return None


def export_experiment(name: str) -> list[dict] | None:
    """Export an experiment's runs BY ID (name resolution is unreliable). Looks up
    the id we stored at create time."""
    eid = _load_state().get("experiments", {}).get(name)
    if not eid:
        return None
    res = _ax("experiments", "export", eid, "--stdout")
    if res and res.returncode == 0:
        try:
            return json.loads(res.stdout or "[]")
        except json.JSONDecodeError:
            return None
    return None


# ─────────────────────────────────────────────────────────────────────────────
# Build experiment-run rows from RunResults + local eval outputs
# ─────────────────────────────────────────────────────────────────────────────
def build_run_rows(runs: list[RunResult], functional: dict, triggered: dict) -> list[dict]:
    """Aggregate trials into ONE experiment run per (case, arm).

    AX experiment pass-through columns require a CONSISTENT, NON-NULL schema
    across all rows (a null or a missing key → 400 Bad Request), so every row
    below carries every column and coerces missing values to sentinels
    ("n/a" / -1.0). One row per example also matches AX's experiment data model.

    functional: (case_id, arm, trial) -> EvaluationResult; triggered: same key -> bool.
    """
    import statistics
    from collections import defaultdict

    groups: dict[tuple[str, str], list[RunResult]] = defaultdict(list)
    for r in runs:
        groups[(r.case_id, r.arm)].append(r)

    rows = []
    for (cid, arm), rs in groups.items():
        rs = sorted(rs, key=lambda x: x.trial)
        fes = [functional[(cid, arm, x.trial)] for x in rs if (cid, arm, x.trial) in functional]
        scores = [fe.score for fe in fes]
        trig = any(bool(triggered.get((cid, arm, x.trial))) for x in rs)
        pass_rate = (sum(1 for s in scores if s >= 1) / len(scores)) if scores else -1.0
        rows.append({
            "case_id": cid,
            "arm": arm,
            "trials": len(rs),
            "output": rs[0].final_output or "",
            "eval1_triggered": bool(trig),
            "eval2_label": (fes[0].label if fes else "n/a"),
            "eval2_pass_rate": float(round(pass_rate, 4)),
            "eval2_explanation": (fes[0].explanation if fes else ""),
            "eval4_tokens_p50": float(statistics.median([x.tokens_total for x in rs])),
            "eval4_turns_mean": float(round(statistics.mean([x.turns for x in rs]), 2)),
            "eval4_wall_clock_mean": float(round(statistics.mean([x.wall_clock_s for x in rs]), 2)),
            "eval4_est_cost_usd": float(round(statistics.mean([x.est_cost_usd for x in rs]), 6)),
        })
    return rows


def push_experiments(skill: str, cases: list[dict], runs: list[RunResult],
                     functional: dict, triggered: dict) -> dict:
    """Best-effort: ensure profile + dataset, create one experiment per arm.
    Returns {arm: experiment_name} for the ones that succeeded."""
    out: dict[str, str] = {}
    if not ax_available():
        print("[ax] credentials/CLI unavailable — AX experiments skipped (local gate still authoritative)")
        return out
    if not ensure_profile():
        print("[ax] could not create profile — AX experiments skipped")
        return out
    dataset_id = ensure_dataset(skill, cases)
    if not dataset_id:
        print("[ax] could not ensure dataset — AX experiments skipped")
        return out

    rows = build_run_rows(runs, functional, triggered)
    by_arm: dict[str, list[dict]] = {}
    for row in rows:
        by_arm.setdefault(row["arm"], []).append(row)
    for arm, arm_rows in by_arm.items():
        name = create_experiment(skill, arm, dataset_id, arm_rows)
        if name:
            out[arm] = name
            print(f"[ax] experiment created: {name} ({len(arm_rows)} runs)")
    return out
