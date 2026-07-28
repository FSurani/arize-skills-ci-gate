"""Harness runner: one (case, skill, arm, trial) -> a normalized RunResult.

Two execution paths:

  REAL  (default when creds + SDK present): a sandboxed headless Claude Code run
        via **ClaudeSDKClient** (plan §4). Standardized on ClaudeSDKClient for
        stream capture of per-turn / per-tool events. Tracing flows to AX through
        the coding-harness-tracing plugin's settings.json hooks (written by
        tracing.write_sandbox_settings). Model pinned to HARNESS_MODEL (Sonnet).

  MOCK  (--mock, or auto when SDK/creds absent): a deterministic simulator that
        reproduces the engineered variant deltas (context §2) WITHOUT spending
        tokens, so ci/gate.py and the report run end-to-end offline. For
        api-helper it writes a realistic mock-API request log + output files that
        the REAL verifiers score, so Eval 2's verifier path is genuinely
        exercised. Clearly labeled in result metadata as mock.

Sandbox safety (plan §7, §12): every run gets a fresh disposable workdir; the
skill installs at .claude/skills/<name>/SKILL.md; settings.local.json carries the
AX env block + a dangerous-command denylist. The harness never writes outside its
workdir.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

HARNESS_DIR = Path(__file__).resolve().parent
REPO = HARNESS_DIR.parent
sys.path.insert(0, str(REPO / "gates"))
sys.path.insert(0, str(HARNESS_DIR))

from common import RunResult, ToolSpan, skill_marker_path  # noqa: E402
import tracing  # noqa: E402

HARNESS_MODEL = os.environ.get("HARNESS_MODEL", "claude-haiku-4-5-20251001")
TIMEOUT_S = 600
MAX_TURNS = 40

# Sandboxes MUST live OUTSIDE the repo — otherwise a real agent walks up the tree,
# finds the real mockapi/.venv/skills, and works around the harness (isolation
# failure, plan §12). Default to a temp dir; callers may override.
DEFAULT_WORK_ROOT = Path(os.environ.get("SKILLS_EVAL_WORK_ROOT",
                                        str(Path(tempfile.gettempdir()) / "gic-skills-eval-runs")))

SKILL_DIRS = {
    ("api-helper", "variant_a"): REPO / "skills/api-helper/variant_a",
    ("api-helper", "variant_b"): REPO / "skills/api-helper/variant_b",
    ("story-writer", "v1"): REPO / "skills/story-writer",
}


# ─────────────────────────────────────────────────────────────────────────────
# Sandbox setup (shared by both paths)
# ─────────────────────────────────────────────────────────────────────────────
def setup_sandbox(case: dict, skill: str, arm: str, work_root: Path) -> Path:
    sandbox = Path(work_root) / skill / arm / case["id"] / f"trial_{case.get('_trial', 0)}"
    if sandbox.exists():
        shutil.rmtree(sandbox)
    sandbox.mkdir(parents=True)

    # copy fixtures
    for fx in case.get("fixtures", []):
        src = REPO / "datasets" / fx
        if src.exists():
            for item in src.iterdir():
                dst = sandbox / item.name
                if item.is_dir():
                    shutil.copytree(item, dst)
                else:
                    shutil.copy2(item, dst)

    # install the skill (except baseline). Both api-helper variants install under
    # the SAME name/path — variant identity lives only in run metadata.
    if arm != "skill_off":
        skill_src = SKILL_DIRS[(skill, arm)]
        dest = sandbox / ".claude" / "skills" / skill / "SKILL.md"
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(skill_src / "SKILL.md", dest)
        # copy any bundled skill files too
        for extra in skill_src.rglob("*"):
            if extra.is_file() and extra.name != "SKILL.md":
                rel = extra.relative_to(skill_src)
                (dest.parent / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(extra, dest.parent / rel)

    # AX tracing settings + denylist
    tracing.write_sandbox_settings(sandbox)
    return sandbox


# ─────────────────────────────────────────────────────────────────────────────
# Mock Orders API lifecycle (needed for api-helper in BOTH paths)
# ─────────────────────────────────────────────────────────────────────────────
def _free_port() -> int:
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def start_mock_api(sandbox: Path, seed: int = 5):
    import subprocess
    port = _free_port()
    log_path = sandbox / "orders_api_log.jsonl"
    env = {**os.environ,
           "ORDERS_API_TOKEN": "gic-secret-token",
           "ORDERS_API_LOG": str(log_path),
           "ORDERS_SEED": str(seed),
           "PORT": str(port)}
    proc = subprocess.Popen(
        [sys.executable, str(REPO / "mockapi" / "orders_api.py")],
        env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    # wait for health
    import urllib.request
    for _ in range(50):
        try:
            urllib.request.urlopen(f"{base}/healthz", timeout=1)
            break
        except Exception:  # noqa: BLE001
            time.sleep(0.1)
    return proc, base, "gic-secret-token"


# ─────────────────────────────────────────────────────────────────────────────
# REAL path — ClaudeSDKClient (runs only when SDK + creds are present).
# Async so many cases run concurrently on one event loop (see run_matrix_async).
# ─────────────────────────────────────────────────────────────────────────────
def _mu_get(mu, *keys):
    """Read a model-usage value trying multiple key spellings.
    ResultMessage.model_usage uses camelCase (inputTokens, cacheReadInputTokens,
    …); the top-level usage dict uses snake_case. Try both."""
    for k in keys:
        v = mu.get(k) if isinstance(mu, dict) else getattr(mu, k, None)
        if v:
            return v
    return 0


async def _run_real_async(case: dict, skill: str, arm: str, sandbox: Path,
                          api_base: str | None, api_token: str | None) -> RunResult:
    from claude_agent_sdk import (ClaudeSDKClient, ClaudeAgentOptions,
                                  AssistantMessage, ResultMessage,
                                  TextBlock, ToolUseBlock)

    env = dict(os.environ)
    env.update(tracing.ax_env())                          # ARIZE_* for the plugin
    if api_base:
        env["ORDERS_API_BASE"] = api_base
        env["ORDERS_API_TOKEN"] = api_token or ""

    opts_kwargs = dict(
        model=HARNESS_MODEL,
        cwd=str(sandbox),
        permission_mode="bypassPermissions",  # safe: disposable sandbox + denylist
        max_turns=MAX_TURNS,
        env=env,
        setting_sources=["user", "project", "local"],
    )
    pdir = tracing.plugin_dir()
    if pdir:
        opts_kwargs["plugins"] = [{"type": "local", "path": str(pdir)}]
    options = ClaudeAgentOptions(**opts_kwargs)

    spans: list[ToolSpan] = []
    final_text, turns, session_id = "", 0, ""
    tin = tout = cache_read = 0
    cost = 0.0
    t0 = time.time()
    try:
        async with ClaudeSDKClient(options=options) as client:
            await client.query(case["prompt"])
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for block in msg.content:
                        if isinstance(block, ToolUseBlock):
                            spans.append(ToolSpan(name=block.name, input=block.input or {}))
                        elif isinstance(block, TextBlock):
                            final_text = block.text
                elif isinstance(msg, ResultMessage):
                    turns = msg.num_turns or turns
                    session_id = msg.session_id or session_id
                    cost = msg.total_cost_usd or 0.0
                    # Sum per-model cumulative model_usage (NOT the last-turn `usage`).
                    # Gated `tokens_input` = fresh input (uncached + cache-creation);
                    # cache-read replay is recorded separately, not gated.
                    for mu in (msg.model_usage or {}).values():
                        tin += (_mu_get(mu, "inputTokens", "input_tokens")
                                + _mu_get(mu, "cacheCreationInputTokens", "cache_creation_input_tokens"))
                        cache_read += _mu_get(mu, "cacheReadInputTokens", "cache_read_input_tokens")
                        tout += _mu_get(mu, "outputTokens", "output_tokens")
                    if tin == 0 and tout == 0:                # fallback to last-turn usage
                        u = msg.usage or {}
                        tin = (u.get("input_tokens", 0) or 0) + (u.get("cache_creation_input_tokens", 0) or 0)
                        tout = u.get("output_tokens", 0) or 0
                        cache_read = u.get("cache_read_input_tokens", 0) or 0
                    if msg.result:
                        final_text = msg.result
    except Exception as e:  # noqa: BLE001 — a crashed/timed-out run scores as a failed run
        final_text = final_text or f"[harness error] {e}"

    return RunResult(
        case_id=case["id"], skill=skill, arm=arm, trial=case.get("_trial", 0),
        session_id=session_id,
        tool_spans=spans, final_output=final_text, turns=turns,
        tokens_input=tin, tokens_output=tout, tokens_cache_read=cache_read,
        wall_clock_s=round(time.time() - t0, 2),
        est_cost_usd=cost or tracing.est_cost_usd(tin, tout),
        workspace_dir=str(sandbox),
    )


# ─────────────────────────────────────────────────────────────────────────────
# MOCK path — deterministic simulator (engineered deltas; no tokens)
# ─────────────────────────────────────────────────────────────────────────────
# Correct answers mirror mockapi/orders_api.py with ORDERS_SEED=5.
_ANSWERS = {
    "t01": ("count.txt", "5"), "t02": ("customer.txt", "Globex"),
    "t03": ("new_id.txt", "ord_0005"), "t04": ("status.txt", "shipped"),
    "t05": ("open_count.txt", "3"), "t06": ("sku.txt", "WIDGET-3"),
    "t07": ("ids.txt", "\n".join(f"ord_{i:04d}" for i in range(5))),
    "t08": ("created_status.txt", "open"), "t12": ("max_order.txt", "ord_0003"),
    "e02": ("status_code.txt", "422"),
    "t09": ("acme.txt", "1"), "t10": ("total_units.txt", "11"),
    "e01": ("out.txt", "NOT_FOUND"),
}
# wrong outputs a flawed run would write (e.g. only page 1 seen, or 401)
_WRONG = {
    "t01": "2", "t05": "1", "t07": "ord_0000\nord_0001", "t12": "ord_0001",
    "t04": "unknown", "e02": "500", "t09": "0", "t10": "3", "e01": "ERROR",
    "t02": "", "t06": "", "t03": "", "t08": "", "e01_": "",
}
# variant_a passes simple single-GET/POST cases, fails the ones needing
# pagination / retry / validation → visible functional delta vs variant_b.
_VARIANT_A_FAILS = {"t01", "t04", "t05", "t07", "t12", "e02", "t09", "t10"}


def _good_log(case_id: str) -> list[dict]:
    tok = {"X-Org-Token": "gic-secret-token"}
    if case_id in ("t01", "t05", "t07", "t12", "t09", "t10"):
        return [{"method": "GET", "path": "/orders", "headers": tok, "status": 200, "page_start": s}
                for s in (0, 2, 4)]
    if case_id == "t02":
        return [{"method": "GET", "path": "/orders/ord_0001", "headers": tok, "status": 200}]
    if case_id == "t06":
        return [{"method": "GET", "path": "/orders/ord_0002", "headers": tok, "status": 200}]
    if case_id == "t04":
        return [{"method": "GET", "path": "/orders/ord_0003", "headers": tok, "status": 503},
                {"method": "GET", "path": "/orders/ord_0003", "headers": tok, "status": 200}]
    if case_id == "e01":
        return [{"method": "GET", "path": "/orders/ord_9999", "headers": tok, "status": 404}]
    if case_id == "t03":
        return [{"method": "POST", "path": "/orders", "headers": tok,
                 "json": {"customer": "ACME", "sku": "WIDGET-1", "units": 2},
                 "status": 201, "created": "ord_0005"}]
    if case_id == "t08":
        return [{"method": "POST", "path": "/orders", "headers": tok,
                 "json": {"customer": "Globex", "sku": "WIDGET-2", "units": 5},
                 "status": 201, "created": "ord_0005"}]
    if case_id == "e02":
        return [{"method": "POST", "path": "/orders", "headers": tok,
                 "json": {"customer": "Initech", "sku": "WIDGET-3"}, "status": 422,
                 "missing": ["units"]}]
    return []


def _bad_log(case_id: str) -> list[dict]:
    # skill_off / a flawed arm: wrong auth convention → 401, no useful data
    return [{"method": "GET", "path": "/orders", "headers": {"Authorization": "Bearer x"}, "status": 401}]


_STORY_GOOD = (
    "**Title:** Self-service password reset\n\n"
    "**Story:** As a returning customer, I want to reset my own password without "
    "contacting support so that I can regain access quickly at any hour.\n\n"
    "**Acceptance Criteria:**\n"
    "- Given I am on the login page, when I click 'Forgot password', then I receive a reset link by email.\n"
    "- Given a valid reset link, when I set a new password meeting the policy, then I can log in with it.\n"
    "- Given an expired reset link, when I open it, then I am told it expired and offered a new one.\n\n"
    "**Notes:** Reset links expire after 30 minutes."
)
_STORY_BAD = "Add a password reset feature. Users should be able to reset passwords."

# Per-case "good run" outputs, keyed by case id — the story-writer analogue of
# api-helper's _ANSWERS. A faithful mock of a GOOD run must produce output that
# actually matches EACH prompt's rubric (the real hub judge scores per case), not
# one canned story reused everywhere. Falls back to _STORY_GOOD for unknown ids.
_STORY_ANSWERS = {
    "s01": _STORY_GOOD,
    "s02": (
        "**Title:** Flag suspicious transactions for review\n\n"
        "**Story:** As a fraud analyst, I want to flag a suspicious transaction for manual "
        "review so that a reviewer can investigate it before funds settle.\n\n"
        "**Acceptance Criteria:**\n"
        "- Given I am viewing a transaction, when I mark it as suspicious with a reason, then it is added to the manual-review queue.\n"
        "- Given a transaction I flagged, when I open the review queue, then it appears with my reason and a timestamp.\n"
        "- Given an already-flagged transaction, when I view it, then it shows a 'pending review' status.\n\n"
        "**Notes:** Flagging does not itself block or reverse the transaction."
    ),
    "s03": (
        "**Title:** Save multiple shipping addresses\n\n"
        "**Story:** As a returning customer, I want to save several shipping addresses so that "
        "I can pick the right one at checkout without retyping it.\n\n"
        "**Acceptance Criteria:**\n"
        "- Given I am in my address book, when I add a new shipping address, then it is saved to my account.\n"
        "- Given I have saved addresses, when I open the address book, then all of them are listed.\n"
        "- Given multiple saved addresses, when I check out, then I can select which one to ship to.\n\n"
        "**Notes:** Out of scope: billing addresses."
    ),
    "s04": (
        "**Title:** Fingerprint login on mobile\n\n"
        "**Story:** As a mobile app user, I want to log in with my fingerprint so that I can "
        "access my account faster without typing my password.\n\n"
        "**Acceptance Criteria:**\n"
        "- Given fingerprint login is enabled, when I open the app and scan a registered fingerprint, then I am logged in.\n"
        "- Given an unrecognized fingerprint, when I scan it, then I am denied and prompted to try again.\n"
        "- Given fingerprint is unavailable, when I reach the login screen, then I can fall back to password login.\n\n"
        "**Notes:** Fingerprint data stays on the device."
    ),
    "s05": (
        "**Title:** Earn loyalty points on purchases\n\n"
        "**Story:** As a loyalty program member, I want to earn points on my purchases so that I am rewarded for shopping.\n\n"
        "**Acceptance Criteria:**\n"
        "- Given I am a member, when a purchase completes, then points are added to my balance based on the amount spent.\n"
        "- Given a refunded purchase, when the refund settles, then the corresponding points are removed.\n\n"
        "---\n\n"
        "**Title:** Progress through membership tiers\n\n"
        "**Story:** As a loyalty program member, I want to move up membership tiers so that I unlock better benefits.\n\n"
        "**Acceptance Criteria:**\n"
        "- Given I cross a tier's point threshold, when my balance updates, then my tier is upgraded.\n"
        "- Given a new tier, when it takes effect, then its benefits become available to me.\n\n"
        "---\n\n"
        "**Title:** Redeem rewards with points\n\n"
        "**Story:** As a loyalty program member, I want to redeem my points for rewards so that I get tangible value from them.\n\n"
        "**Acceptance Criteria:**\n"
        "- Given enough points, when I redeem a reward, then my balance is reduced and the reward is issued.\n"
        "- Given insufficient points, when I try to redeem, then I am told how many more I need.\n\n"
        "---\n\n"
        "**Title:** Loyalty email notifications\n\n"
        "**Story:** As a loyalty program member, I want email updates about my points and rewards so that I stay informed.\n\n"
        "**Acceptance Criteria:**\n"
        "- Given I earn points, when the balance updates, then I receive an email summarizing the change.\n"
        "- Given a reward becomes available, when it is unlocked, then I am notified by email.\n\n"
        "**Notes:** Members can opt out of loyalty emails."
    ),
    "s06": (
        "**Title:** Bulk-export user data to CSV\n\n"
        "**Story:** As an admin, I want to bulk-export user data to a CSV file so that I can "
        "analyze it in external tools.\n\n"
        "**Acceptance Criteria:**\n"
        "- Given I am on the users page, when I start a bulk export, then a CSV of user records is generated.\n"
        "- Given a completed export, when it is ready, then I can download the CSV file.\n"
        "- Given an export with no matching users, when it runs, then I get a CSV with only the header row.\n\n"
        "**Notes:** Scope is limited to CSV export of existing user data."
    ),
}


def _jitter(case_id: str, lo: int, hi: int) -> int:
    h = int(hashlib.sha1(case_id.encode()).hexdigest(), 16)
    return lo + (h % max(1, (hi - lo)))


def run_mock(case: dict, skill: str, arm: str, sandbox: Path) -> RunResult:
    cid = case["id"]
    triggered = arm != "skill_off"  # skill loads on non-baseline arms...
    if skill == "api-helper" and arm == "variant_a":
        triggered = True             # broad description → loads even on negatives (FP)
    elif skill == "api-helper" and arm == "variant_b":
        triggered = bool(case.get("should_trigger"))  # tight description → correct
    elif skill == "story-writer":
        triggered = bool(case.get("should_trigger")) if arm != "skill_off" else False

    spans: list[ToolSpan] = []
    if triggered:
        # match the real Claude Code skill-load signal: a Skill tool-use
        spans.append(ToolSpan(name="Skill", input={"skill": skill}))

    # decide functional success + write artifacts
    if skill == "api-helper" and case.get("check"):
        if arm == "variant_b":
            passing = True
        elif arm == "variant_a":
            passing = cid not in _VARIANT_A_FAILS
        else:  # skill_off
            passing = False
        fname, val = _ANSWERS[cid]
        if passing:
            (sandbox / fname).write_text(val)
            log = _good_log(cid)
        else:
            (sandbox / fname).write_text(_WRONG.get(cid, ""))
            log = _bad_log(cid)
        (sandbox / "orders_api_log.jsonl").write_text("\n".join(json.dumps(r) for r in log))
        spans.append(ToolSpan(name="Bash", input={"command": f"python solve_{cid}.py"}))
        final = f"Done. Wrote {fname}."
    elif skill == "story-writer":
        final = (_STORY_ANSWERS.get(cid, _STORY_GOOD)) if arm != "skill_off" else _STORY_BAD
    else:
        final = "(no functional task)"

    # engineered efficiency deltas: good arms (variant_b, story-writer v1) are
    # cheap; variant_a is middling; the skill_off baseline is the most expensive.
    if arm in ("variant_b", "v1"):
        turns, base_tok = _jitter(cid, 3, 6), _jitter(cid, 7000, 12000)
    elif arm == "variant_a":
        turns, base_tok = _jitter(cid, 7, 14), _jitter(cid, 22000, 30000)
    else:  # skill_off
        turns, base_tok = _jitter(cid, 10, 18), _jitter(cid, 32000, 42000)
    tin, tout = int(base_tok * 0.7), int(base_tok * 0.3)

    return RunResult(
        case_id=cid, skill=skill, arm=arm, trial=case.get("_trial", 0),
        session_id=f"mock-{skill}-{arm}-{cid}-{case.get('_trial', 0)}",
        tool_spans=spans, final_output=final, turns=turns,
        tokens_input=tin, tokens_output=tout,
        wall_clock_s=round(base_tok / 1500, 2),
        est_cost_usd=tracing.est_cost_usd(tin, tout),
        workspace_dir=str(sandbox),
    )


# ─────────────────────────────────────────────────────────────────────────────
# Orchestration
# ─────────────────────────────────────────────────────────────────────────────
def _auto_mock() -> bool:
    """Fall back to mock when we can't do a real run."""
    if not (os.environ.get("ANTHROPIC_API_KEY") and tracing.credentials_present()):
        return True
    try:
        import claude_agent_sdk  # noqa: F401
        return False
    except ImportError:
        return True


async def run_case_async(case: dict, skill: str, arm: str, trial: int, work_root: Path,
                         mock: bool | None = None) -> RunResult:
    """Async core: one (case, skill, arm, trial) -> RunResult. Awaitable so many
    cases run concurrently on one event loop (run_matrix_async / the AX
    experiments.run concurrency executor). Blocking setup/mock-API/mock-run steps
    are pushed to threads so they don't stall the loop."""
    import asyncio
    case = {**case, "_trial": trial}
    sandbox = await asyncio.to_thread(setup_sandbox, case, skill, arm, work_root)

    use_mock = _auto_mock() if mock is None else mock

    api_proc = api_base = api_token = None
    needs_real_api = skill == "api-helper" and not use_mock
    try:
        if needs_real_api:
            api_proc, api_base, api_token = await asyncio.to_thread(start_mock_api, sandbox)
        if use_mock:
            result = await asyncio.to_thread(run_mock, case, skill, arm, sandbox)
        else:
            try:
                result = await asyncio.wait_for(
                    _run_real_async(case, skill, arm, sandbox, api_base, api_token), TIMEOUT_S)
            except Exception as e:  # noqa: BLE001 — timeout/crash → a failed run
                result = RunResult(case_id=case["id"], skill=skill, arm=arm, trial=trial,
                                   final_output=f"[harness error] {e}", workspace_dir=str(sandbox))
    finally:
        if api_proc:
            api_proc.terminate()

    await asyncio.to_thread(
        (sandbox / "result.json").write_text, json.dumps(result.to_dict(), indent=2))
    return result


def run_case(case: dict, skill: str, arm: str, trial: int, work_root: Path,
             mock: bool | None = None) -> RunResult:
    """Sync wrapper around run_case_async (CLI / single-case callers)."""
    import asyncio
    return asyncio.run(run_case_async(case, skill, arm, trial, work_root, mock))


async def run_matrix_async(triples: list[tuple[dict, str, int]], skill: str, work_root: Path,
                           mock: bool | None = None, concurrency: int = 4) -> list[RunResult]:
    """Run many (case, arm, trial) triples CONCURRENTLY with a bounded semaphore.

    Mirrors the Arize AX experiments executor (N consumer coroutines + gather,
    per client.experiments.run(concurrency=N)) — we hand-roll it here because our
    'task' spawns a headless Claude Code agent per example."""
    import asyncio
    sem = asyncio.Semaphore(concurrency)

    async def _one(case, arm, trial):
        async with sem:
            return await run_case_async(case, skill, arm, trial, work_root, mock=mock)

    return await asyncio.gather(*[_one(c, a, t) for (c, a, t) in triples])


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skill", required=True)
    ap.add_argument("--arm", required=True)
    ap.add_argument("--case-json", required=True, help="a single case row as JSON")
    ap.add_argument("--trial", type=int, default=0)
    ap.add_argument("--work-root", default=str(DEFAULT_WORK_ROOT))
    ap.add_argument("--mock", action="store_true")
    args = ap.parse_args()
    case = json.loads(args.case_json)
    res = run_case(case, args.skill, args.arm, args.trial, Path(args.work_root),
                   mock=True if args.mock else None)
    print(json.dumps(res.to_dict(), indent=2))
