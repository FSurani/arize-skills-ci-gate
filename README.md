# Gating AI-Authored Skills in CI — on Arize AX

A reference implementation for gating AI-authored **agent skills** in CI/CD, built on
**Arize AX**. The plumbing is deliberately small — the interesting engineering is the
**evaluators**: a security scan, "did the skill fire when it should," "is the output
actually good," and efficiency.

It covers both **verifiable** skills (deterministic checks) and **non-verifiable** ones
(LLM rubric judge), and runs **end-to-end offline** via a mock harness (no tokens) or
against real headless **Claude Code** runs with real Arize experiments + traces.

## The shape

Provision Arize once, then gate on every change:

```
experiments/setup.py     →  create the evaluators (Eval Hub) + datasets in Arize   (run once)
ci/gate.py               →  eval0 → run skill (traced) → Arize experiment → score → call  (per run)
```

`ci/gate.py` reads top-to-bottom:

```python
eval0(skill)                       # 1. structural + SECURITY scan → bounce bad skills
runs = run_harness(skill)          # 2. run the skill on its cases, traced to Arize
ds, exp = hub.run_experiment(runs) # 3. push an experiment; Eval Hub scores it server-side
m = hub.read_metrics(ds, exp)      # 4. read the scores back from Arize
sys.exit(0 if m.meet(thresholds) else 1)   # 5. make the call
```

## The evals (where the richness lives) — `experiments/eval_hub.py`

Each is an **Arize evaluator**, created once and run **server-side**:

| Evaluator | Kind | What it decides |
|---|---|---|
| `cc-eval1-trigger` | code | Did the skill fire exactly when it should? (over-eager description = false positives) |
| `cc-eval2-verifier` | code | Verifiable skills: correct answer + correct auth convention |
| `cc-eval2-rubric` | LLM template | Non-verifiable skills: INVEST-style quality judged by an LLM (good/bad + explanation) |
| `cc-eval4-efficiency` | code | Works-but-wasteful gotcha: token budget |

Plus **Eval 0** (`gates/eval0_structural.py`) — the structural + prompt-injection/secret
security scan that runs first, locally, and short-circuits the pipeline.

Adding a new eval is ~10 lines: add an entry to `EVALUATORS` (a code string or an LLM
template) + list it in `SKILL_EVALS` + map its columns in `MAPPINGS`. *(Agent-as-a-Judge
over the trace is a natural next one.)*

## What's here

| Path | Role |
|---|---|
| `skills/api-helper/{variant_a,variant_b}` | **Verifiable** skill, two A/B variants (engineered to differ) |
| `skills/story-writer` | **Non-verifiable** skill (rubric-judged) |
| `skills/_bad_fixture` | Deliberately malicious/malformed skill for the Eval 0 security test |
| `datasets/*.jsonl` | Skill cases (task / hard-negative / edge) |
| `experiments/eval_hub.py` | The evaluators + Arize plumbing (provision, experiment, score, read) |
| `experiments/setup.py` | Provision Arize once: create evaluators + datasets |
| `gates/eval0_structural.py` | Eval 0 — structural + security scan (local) |
| `harness/run_case.py`, `tracing.py` | Sandboxed skill runner (real `ClaudeSDKClient` + offline mock) + Arize tracing |
| `mockapi/orders_api.py` | Local mock of the internal Orders API |
| `ci/gate.py`, `ci/thresholds.yaml` | The CI gate + thresholds |
| `ci/skill-gate.yml` | **The reusable workflow** (copy into any skills repo) |
| `tests/` | Eval 0 security test + evaluator-definition test |

## Prerequisites

- **Python 3.11+**
- **Arize AX CLI `ax` — v0.27** (`pip install arize-ax-cli`) + an Arize account (API key + space id).
- **Anthropic API key** — only for real runs + the LLM judge. Omit to stay in offline `--mock`.
- **(Real runs only)** Claude Code + the Arize coding-harness-tracing plugin.

## Setup

```bash
# 1. venv + deps (Arize SDK + ax CLI 0.27 + Claude Agent SDK + tests)
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt

# 2. keys
cp .env.example .env    # ARIZE_API_KEY, ARIZE_SPACE_ID, ARIZE_AI_INTEGRATION_ID, ANTHROPIC_API_KEY
set -a && . ./.env && set +a

# 3. provision Arize once — creates the Eval Hub evaluators + the datasets
python experiments/setup.py
```

For **real** runs + tracing (skip to stay offline):

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude plugin marketplace add Arize-ai/coding-harness-tracing
claude plugin install claude-code-tracing@coding-harness-tracing
```

## Run it

```bash
# Gate a skill: eval0 → harness → Arize experiment → hub scores → verdict → exit code
python ci/gate.py --skill api-helper --mock
python ci/gate.py --skill api-helper --variant variant_a --mock   # the worse variant → FAIL
python ci/gate.py --skill story-writer --mock

# Tests
python -m pytest tests/ -q
```

Everything works with `--mock` (no keys/tokens). Drop `--mock` once keys + Claude Code are
installed for real runs. Flags: `--skill`, `--variant`, `--mock`, `--max-cases N`, `--dry-run`.

## The reusable workflow

`ci/skill-gate.yml` is a `workflow_call` reusable workflow: on a PR it provisions Arize
(idempotent) and runs `ci/gate.py` on the changed skill. Copy it into any skills repo's
`.github/workflows/` and set `ARIZE_API_KEY`, `ARIZE_SPACE_ID`, `ARIZE_AI_INTEGRATION_ID`,
and `ANTHROPIC_API_KEY` as repo/org secrets. Without `ANTHROPIC_API_KEY` it runs in mock mode.

## Design notes

- **Arize is the system of record.** Evaluators are created in the Eval Hub and run
  server-side; the gate reads their scores back and thresholds on them. Experiments are
  `cc-`-named and append-only — the experiment history *is* the skill's version history.
- **One dataset per skill.** `setup.py` creates a stable `cc-<skill>` dataset; each gate
  run adds an experiment against it (id persisted in `report/out/hub_state.json`).
- **Functional pass-rate counts positives only** — hard negatives have no functional task.
- **Eval 0 is deliberately simple** (pattern-based) — it shows *where* the security gate
  lives. Production should layer a dedicated scanner / guardrail model here.
- **The two api-helper variants are engineered to differ** — variant_a is intentionally
  worse (broad description → false-positive triggers, terse body → lower pass rate).
- **Mock harness** (`--mock`) reproduces the engineered results deterministically with no
  tokens, and still writes a real mock-API request log so the verifier is genuinely exercised.

### AX CLI 0.27 notes (handled in `experiments/eval_hub.py`)

- Pass the **base64 space GID** with `-s`; address datasets/experiments **by id**.
- Dataset rows may not use a column named `id` → carry it as `case_id`.
- Experiment run files need a **consistent, non-null column schema** (nulls → 400).
- Export experiments **by id** (`--stdout`, no `--output json`). `evaluators list` caps
  `-l` at 100 (paginate by cursor). Code evaluators run server-side, so each is a
  self-contained code string with a single `return EvaluationResult(...)`.

## Sandbox safety

Headless runs use bypass-permissions ONLY inside a disposable per-run sandbox, with a
`settings.local.json` denylist blocking dangerous shell commands (`rm`, `sudo`, `curl`,
`wget`, `ssh`, network redirects, `git push`) and `WebFetch`/`WebSearch`. The harness
never writes outside its workdir.
