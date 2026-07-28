# Skills Eval & CI Gating Demo — on Arize AX

Gate AI-authored **agent skills** in CI the way teams do it today (structural
validation → trigger evaluation → functional/quality gating + a security scan),
standardized on **Arize AX** and packaged as a **reusable GitHub Actions
workflow**. It handles both **verifiable** skills (deterministic verifiers) and
**non-verifiable** skills (rubric LLM judge) — because a real skills registry has
both.

The demo runs **end-to-end offline** via a deterministic mock harness (no tokens),
and switches to real headless **Claude Code** runs + real **AX experiments** when
credentials are present.

---

## What's here

| Path | Role |
|---|---|
| `skills/api-helper/{variant_a,variant_b}` | **Verifiable** skill, two A/B variants (engineered to differ) |
| `skills/story-writer` | **Non-verifiable** skill (rubric-judged) |
| `skills/_bad_fixture` | Deliberately malicious/malformed skill for the Eval 0 test |
| `datasets/*.jsonl`, `datasets/holdout/` | Cases + a 20% holdout scored once |
| `mockapi/orders_api.py` | Local mock of the internal Orders API |
| `verifiers/api_helper/` | Deterministic per-case verifiers (Eval 2 verifier path) |
| `gates/eval0..eval4` | The five gates (harness-agnostic — no Claude-Code imports) |
| `harness/run_case.py` | Sandboxed runner: real `ClaudeSDKClient` path + offline mock path |
| `harness/tracing.py` | AX tracing wiring (settings.json hooks + supplementary OTLP) |
| `experiments/run_experiment.py` | AX experiments via the `ax` CLI (system-of-record) |
| `ci/gate.py`, `ci/thresholds.yaml` | Single CI entrypoint + thresholds |
| `ci/skill-gate.yml` | **The reusable workflow** (copy into any skills repo) |
| `report/summarize.py` | Comparison table + holdout + pairwise + AX link |
| `tests/` | Eval 0 security, Eval 1 trigger, Eval 2 schema, Eval 3 blinding |

The five gates (plan §5): **Eval 0** structural + security (runs first,
short-circuits) · **Eval 1** trigger FP/FN · **Eval 2** functional (verifier *or*
rubric, same schema) · **Eval 3** blinded pairwise (Skill A) + AX Agent-as-a-Judge
· **Eval 4** efficiency (tokens/turns/cost).

---

## Setup (≤10 steps)

```bash
# 1. create + activate a venv (Python 3.11+)
python3 -m venv .venv && . .venv/bin/activate

# 2. install deps (arize SDK + ax CLI + Claude Agent SDK + judge + tests)
pip install -r requirements.txt

# 3. copy env template and fill in your keys
cp .env.example .env    # then edit: ARIZE_API_KEY, ARIZE_SPACE_ID, ANTHROPIC_API_KEY
set -a && . ./.env && set +a
```

For **real** headless runs + tracing you also need Claude Code and the AX tracing
plugin (skip these to stay in offline `--mock` mode):

```bash
# 4. (real runs) install Claude Code
curl -fsSL https://claude.ai/install.sh | bash

# 5. (real runs) install the Arize coding-harness-tracing plugin
claude plugin marketplace add Arize-ai/coding-harness-tracing
claude plugin install claude-code-tracing@coding-harness-tracing
```

---

## Run it

Everything works with `--mock` (no keys, no tokens). Drop `--mock` once keys +
Claude Code are installed to do real runs.

```bash
# Gate a skill (Evals 0-4 → verdict table → exit code)
python ci/gate.py --skill api-helper --mock
python ci/gate.py --skill story-writer --mock

# Final report (comparison table + holdout scored once + pairwise + AX link)
python report/summarize.py --skill api-helper --mock

# Unit tests
python -m pytest tests/ -q
```

Cost-control flags on every entrypoint (plan §12): `--mock`, `--max-cases N`,
`--dry-run`, `--local` (gate on locally computed evals instead of AX hub scores;
`--no-ax` is a kept alias), and `ARIZE_DRY_RUN=true` for tracing smoke tests.

**Parallelism:** harness runs execute concurrently via asyncio — `--concurrency N`
(default 4, or `SKILLS_EVAL_CONCURRENCY`). This mirrors the Arize AX
`client.experiments.run(concurrency=N)` async executor (N consumer coroutines +
`asyncio.gather`); each run gets its own free-port mock API + disposable sandbox,
so they're isolation-safe in parallel.

**Token accounting (real runs):** the gated `tokens_input` is *fresh* input
(uncached + cache-creation) summed from `ResultMessage.model_usage` (camelCase
keys); cache-read replay is recorded separately (`tokens_cache_read`) and NOT
gated, since it's cheap context replay. `est_cost_usd` comes from the SDK's exact
`total_cost_usd`.

---

## The 6-beat demo script (plan §10)

1. **The bad skill bounces at the door.**
   `python ci/gate.py --skill _bad_fixture --mock` → Eval 0 fails in seconds with
   the injection + secret + undeclared-network findings. Exit 1.
2. **Why the skill exists.** Compare the `skill_off` baseline column (0% pass) to
   the with-skill columns in the report / AX traces.
3. **Two variants, one verdict.** `python ci/gate.py --skill api-helper --mock` →
   variant_b beats variant_a on pass rate **and** tokens; variant_a's broad
   description trips every hard negative (FP 1.00) while variant_b's tight
   description doesn't (FP 0.00).
4. **Judges for the unverifiable.** `python ci/gate.py --skill story-writer --mock`
   → rubric judge verdicts (+ AX Agent-as-a-Judge on the same runs).
5. **The gate in CI.** `--arm variant_a` fails / `--arm variant_b` passes; the
   verdict table + `thresholds.yaml` are the closing artifact. Holdout scores
   appear here, once, to pre-empt the overfitting question.
6. **(Stretch, not built)** self-improvement loop — see plan §9.

---

## The reusable workflow

`ci/skill-gate.yml` is a `workflow_call` reusable workflow: it detects skills
changed in a PR and runs `ci/gate.py` on each, standardized on AX. Copy it into a
skills repo's `.github/workflows/` and call it (example caller at the bottom of
the file). Set `ARIZE_API_KEY`, `ARIZE_SPACE_ID`, and `ANTHROPIC_API_KEY` as repo
or org secrets. Without `ANTHROPIC_API_KEY` it runs in mock mode.

---

## Eval Hub (AX-native evaluators) — `experiments/eval_hub.py`

Two complementary flows ship here:
- **`experiments/eval_hub.py`** — the AX-native story: evaluators are **created in
  Arize** (`cc-` prefixed), referenced by **ID**, and **run server-side by Arize**
  over the experiments — the Evaluator Hub, not local calls.
- **`ci/gate.py`** — the CI gate. By default it **builds on the hub**: it creates the
  `cc-` experiments (via `eval_hub`), lets Arize score them, then **reads the
  `eval.cc_*` scores back from AX and thresholds on them** — AX is the system of
  record end-to-end. `--local` computes the evals locally instead (offline /
  CI-resilience fallback; used automatically if the AX round-trip fails).

```bash
python experiments/eval_hub.py --skill api-helper            # mock harness (no tokens)
python experiments/eval_hub.py --skill story-writer --real   # real headless Claude Code
```
It runs the harness (one experiment per arm: baseline / variant_a / variant_b),
then creates + triggers four hub evaluators and reads their scores back:

| Evaluator | Kind | Covers |
|---|---|---|
| `cc-eval1-trigger` | code | Eval 1 — triggered vs should_trigger |
| `cc-eval2-verifier` | code | Eval 2 verifier — answer + auth (api-helper) |
| `cc-eval2-rubric` | LLM template | Eval 2 rubric — INVEST judge (story-writer) |
| `cc-eval4-efficiency` | code | Eval 4 — token budget |

Recommended split: deterministic evals may run local or hub; **LLM-as-judge always
in the hub** (managed provider creds via an AI integration, versioned prompt
templates, no keys in CI). The LLM evaluator reuses an existing Anthropic AI
integration in the space — `--ai-integration-id` is required for template
evaluators (Arize runs the model server-side), **not** for code evaluators.

See `HANDOFF.md` for the full AX-CLI-v0.27 gotcha list (space-id must be the
base64 GID; `spaces list`/`tasks *-run` parse bugs → poll via the raw `/v2` API;
`evaluators list` caps `-l` at 100; code evaluators forbid nested non-EvaluationResult
returns; experiment columns map by bare top-level name).

## Design notes, deviations & caveats

- **AX is the system of record for the gate (Option 3+).** By default `gate.py`
  thresholds against the **hub scores read back from AX** (`eval.cc_*` on the `cc-`
  experiments Arize scored server-side), so the CI gate consumes AX rather than
  recomputing — consistent with the experiment/trace beats. A **local** threshold
  compute (`--local`) is kept as an offline / CI-resilience fallback and is used
  automatically if the AX round-trip fails. Experiments are `cc-` named,
  append-only, never deleted. (The earlier best-effort non-`cc` `skill@hash` push
  is retired.) Functional pass-rate counts positives only — negatives have no
  functional task, and the verifier labels them fail.
- **Eval 0 is deliberately simple** (pattern-based). It demonstrates *where* the
  security gate lives; production should layer a dedicated scanner / guardrail
  model here. It flags prompt injection, credential/secret reads, exfiltration
  language, embedded secrets, obfuscated payloads, and undeclared network calls in
  bundled scripts — while NOT flagging a skill referencing its own auth env var.
- **The two api-helper variants are engineered to differ** (context §2). variant_a
  is intentionally worse; do not "equalize" them.
- **Holdout is scored exactly once**, in `report/summarize.py`, never during
  iteration.
- **Both variants install under the same skill name/path**; variant identity lives
  only in run metadata, which keeps the pairwise judge blindable (unit-tested).
- **Tracing** relies on the coding-harness-tracing plugin's `settings.json` hooks,
  which fire for both the `claude` CLI and the Agent SDK (see plan §0 build-time
  corrections). `run_case.py` uses `ClaudeSDKClient` for stream capture.
- **Verified against live tooling (2026-07):** `ax` CLI v0.25, arize SDK v8.32;
  models `claude-haiku-4-5-20251001` (harness — cheap + surfaces more failures) /
  `claude-sonnet-4-6` (judge). Re-verify via
  `ax <cmd> --help` and https://arize.com/docs/ax if something breaks.
- **AX CLI v0.25 quirks (live-verified, handled in `experiments/run_experiment.py`;**
  **full notes in plan §7):** the space id form differs by subcommand (writes want
  the base64 GID, name-based reads want the decoded `Space:N:x`) so we address
  datasets/experiments **by id**; dataset examples can't use a column named `id`
  (→ `case_id`); experiment run files need a **consistent, non-null column schema**
  (nulls/missing keys → 400), so trials are aggregated to one run per example;
  experiment export is **by id**. Dataset/experiment ids are persisted in
  `report/out/ax_state.json` for idempotency (never delete — plan §6).

## Sandbox safety

Headless runs use bypass-permissions ONLY inside a disposable per-run sandbox
workdir, with a `settings.local.json` denylist blocking dangerous shell commands
(`rm`, `sudo`, `curl`, `wget`, `ssh`, network redirects, `git push`) and
`WebFetch`/`WebSearch`. The harness never writes outside its workdir.
