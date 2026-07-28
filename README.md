# Gating AI-Authored Skills in CI — on Arize AX

A reference implementation for gating AI-authored **agent skills** in CI/CD,
standardized on **Arize AX** and packaged as a **reusable GitHub Actions workflow**.

It mirrors how teams gate skills today — structural validation → trigger evaluation
→ functional/quality gating, plus a security scan — and covers both **verifiable**
skills (deterministic checks) and **non-verifiable** ones (rubric LLM judge), because
a real skills registry has both.

The demo runs **end-to-end offline** via a deterministic mock harness (no keys, no
tokens), and switches to real headless **Claude Code** runs with real **Arize
experiments and traces** when credentials are present.

---

## What's here

| Path | Role |
|---|---|
| `skills/api-helper/{variant_a,variant_b}` | **Verifiable** skill, two A/B variants (engineered to differ) |
| `skills/story-writer` | **Non-verifiable** skill (rubric-judged) |
| `skills/_bad_fixture` | Deliberately malicious/malformed skill for the Eval 0 security test |
| `datasets/*.jsonl`, `datasets/holdout/` | Cases + a 20% holdout scored once |
| `mockapi/orders_api.py` | Local mock of the internal Orders API |
| `verifiers/api_helper/` | Deterministic per-case verifiers (Eval 2 verifier path) |
| `gates/eval0..eval4` | The five gates (harness-agnostic — no Claude-Code imports) |
| `harness/run_case.py` | Sandboxed runner: real `ClaudeSDKClient` path + offline mock path |
| `harness/tracing.py` | Arize tracing wiring (settings.json hooks + supplementary OTLP) |
| `experiments/eval_hub.py` | Arize datasets/experiments + hub evaluators via the `ax` CLI |
| `ci/gate.py`, `ci/thresholds.yaml` | Single CI entrypoint + thresholds |
| `ci/skill-gate.yml` | **The reusable workflow** (copy into any skills repo) |
| `report/summarize.py` | Comparison table + holdout + pairwise + Arize link |
| `tests/` | Eval 0 security, Eval 1 trigger, Eval 2 schema, Eval 3 blinding |

The five gates: **Eval 0** structural + security (runs first, short-circuits) ·
**Eval 1** trigger FP/FN · **Eval 2** functional (verifier *or* rubric, same schema) ·
**Eval 3** blinded pairwise + Agent-as-a-Judge · **Eval 4** efficiency
(tokens/turns/cost).

---

## Prerequisites

- **Python 3.11+**
- **Arize AX CLI `ax` — v0.27** (`pip install arize-ax-cli`), plus an Arize account
  (API key + space id). The workflows and `experiments/eval_hub.py` are written and
  verified against **CLI 0.27**.
- **Anthropic API key** — only for real headless runs and the LLM judge. Omit it to
  stay entirely in offline `--mock` mode.
- **(Real runs only)** Claude Code + the Arize coding-harness-tracing plugin.

---

## Setup

```bash
# 1. create + activate a venv
python3 -m venv .venv && . .venv/bin/activate

# 2. install deps (Arize SDK + ax CLI 0.27 + Claude Agent SDK + judge + tests)
pip install -r requirements.txt

# 3. copy the env template and fill in your keys
cp .env.example .env    # ARIZE_API_KEY, ARIZE_SPACE_ID, ANTHROPIC_API_KEY
set -a && . ./.env && set +a
```

For **real** headless runs + tracing (skip to stay in offline `--mock` mode):

```bash
# 4. install Claude Code
curl -fsSL https://claude.ai/install.sh | bash

# 5. install the Arize coding-harness-tracing plugin
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

# Final report (comparison table + holdout scored once + pairwise + Arize link)
python report/summarize.py --skill api-helper --mock

# Unit tests
python -m pytest tests/ -q
```

**Flags** (on every entrypoint): `--mock`, `--max-cases N`, `--dry-run`,
`--local` (gate on locally computed evals instead of the Arize hub scores;
`--no-ax` is a kept alias), and `ARIZE_DRY_RUN=true` for tracing smoke tests.

**Parallelism:** harness runs execute concurrently via asyncio — `--concurrency N`
(default 4, or `SKILLS_EVAL_CONCURRENCY`), mirroring the Arize
`client.experiments.run(concurrency=N)` async executor. Each run gets its own
free-port mock API + disposable sandbox, so they're isolation-safe in parallel.

**Token accounting (real runs):** the gated `tokens_input` is *fresh* input
(uncached + cache-creation) summed from `ResultMessage.model_usage`; cache-read
replay is recorded separately and NOT gated. `est_cost_usd` comes from the SDK's
exact `total_cost_usd`.

---

## Demo walkthrough

1. **The bad skill bounces at the door.**
   `python ci/gate.py --skill _bad_fixture --mock` → Eval 0 fails in seconds with
   the injection + secret + undeclared-network findings. Exit 1.
2. **Why the skill exists.** Compare the `skill_off` baseline (fails the task) to
   the with-skill runs in the report / Arize traces.
3. **Two variants, one verdict.** `python ci/gate.py --skill api-helper --mock` →
   variant_b beats variant_a on pass rate **and** tokens; variant_a's broad
   description trips the hard negatives (FP 1.00) while variant_b's tight
   description doesn't (FP 0.00).
4. **Judges for the unverifiable.** `python ci/gate.py --skill story-writer --mock`
   → rubric-judge verdicts with explanations (plus Agent-as-a-Judge on the runs).
5. **The gate in CI.** `--arm variant_a` fails / `--arm variant_b` passes; the
   verdict table + `thresholds.yaml` are the closing artifact. Holdout scores
   appear here, once, to pre-empt the overfitting question.

---

## The reusable workflow

`ci/skill-gate.yml` is a `workflow_call` reusable workflow: it detects skills
changed in a PR and runs `ci/gate.py` on each, standardized on Arize. Copy it into a
skills repo's `.github/workflows/` and call it (example caller at the bottom of the
file). Set `ARIZE_API_KEY`, `ARIZE_SPACE_ID`, and `ANTHROPIC_API_KEY` as repo or org
secrets. Without `ANTHROPIC_API_KEY` it runs in mock mode.

---

## Arize is the system of record

By default `ci/gate.py` builds on the Arize Evaluator Hub:

1. `experiments/eval_hub.py` creates one stable `cc-<skill>` dataset and pushes an
   experiment per arm.
2. The **hub evaluators** — created in Arize, referenced by id — score each
   experiment **server-side**.
3. `gate.py` **reads the `eval.cc_*` scores back from Arize** and thresholds on them,
   then exits 0/1.

`--local` computes the evals locally instead — an offline / CI-resilience fallback,
used automatically if the Arize round-trip fails.

The four hub evaluators:

| Evaluator | Kind | Covers |
|---|---|---|
| `cc-eval1-trigger` | code | Eval 1 — triggered vs should_trigger |
| `cc-eval2-verifier` | code | Eval 2 verifier — answer + auth (api-helper) |
| `cc-eval2-rubric` | LLM template | Eval 2 rubric — INVEST judge (story-writer) |
| `cc-eval4-efficiency` | code | Eval 4 — token budget |

Recommended split: deterministic evals may run local or in the hub; **LLM-as-judge
always in the hub** (managed provider credentials via an AI integration, versioned
prompt templates, no keys in CI). Template evaluators require `--ai-integration-id`
(Arize runs the model server-side); code evaluators do not.

---

## Design notes & caveats

- **Functional pass-rate counts positives only** — hard negatives have no functional
  task and the verifier labels them fail, so they're excluded from the denominator.
- **Experiments are `cc-`-named, append-only, never deleted.** Each gate run appends
  a version record; the experiment history in Arize is the skill's version history.
- **Eval 0 is deliberately simple** (pattern-based) — it demonstrates *where* the
  security gate lives. It flags prompt injection, credential/secret reads,
  exfiltration language, embedded secrets, obfuscated payloads, and undeclared
  network calls in bundled scripts, while NOT flagging a skill that references its
  own auth env var. Production should layer a dedicated scanner / guardrail model
  here.
- **The two api-helper variants are engineered to differ** — variant_a is
  intentionally worse (broad description, terse body). Don't "equalize" them.
- **Holdout is scored exactly once**, in `report/summarize.py`, never during
  iteration.
- **Both variants install under the same skill name/path**; variant identity lives
  only in run metadata, which keeps the pairwise judge blindable (unit-tested).
- **Tracing** relies on the coding-harness-tracing plugin's `settings.json` hooks,
  which fire for both the `claude` CLI and the Agent SDK. `run_case.py` uses
  `ClaudeSDKClient` for per-turn / per-tool stream capture.

### AX CLI 0.27 notes (handled in code)

- Pass the **base64 space GID** with `-s` on every command (name-based reads can
  return empty), and address datasets/experiments **by id**.
- Dataset example rows may not use a column named `id` → carry it as `case_id`.
- Experiment run files need a **consistent, non-null column schema** (nulls or
  missing keys → 400), so the trials for an example are aggregated into one run.
- Export experiments **by id** (`ax experiments export <id> --dataset <ds> -s <GID>
  --stdout`; no `--output json`). `evaluators list` caps `-l` at 100 (paginate by
  cursor). Custom code evaluators must have a single `return EvaluationResult(...)`
  with no nested non-`EvaluationResult` functions.
- Verified tooling (2026-07): `ax` CLI **0.27**, Arize SDK 8.32; models
  `claude-haiku-4-5-20251001` (harness) / `claude-sonnet-4-6` (judge). Re-verify via
  `ax <cmd> --help` and https://arize.com/docs/ax if something changes.

---

## Sandbox safety

Headless runs use bypass-permissions ONLY inside a disposable per-run sandbox
workdir, with a `settings.local.json` denylist blocking dangerous shell commands
(`rm`, `sudo`, `curl`, `wget`, `ssh`, network redirects, `git push`) and
`WebFetch`/`WebSearch`. The harness never writes outside its workdir.
