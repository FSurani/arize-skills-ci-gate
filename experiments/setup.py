#!/usr/bin/env python3
"""experiments/setup.py — provision Arize once.

Creates the Eval Hub evaluators and the per-skill datasets in Arize, so the CI
gate (ci/gate.py) has evals + datasets to run experiments against. Idempotent —
safe to re-run; it reuses anything that already exists.

    python experiments/setup.py

Requires ARIZE_API_KEY, ARIZE_SPACE_ID, and (for the LLM rubric judge)
ARIZE_AI_INTEGRATION_ID in the environment.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import eval_hub as hub  # noqa: E402

SKILLS = ["api-helper", "story-writer"]


def main():
    if not hub.SPACE or not hub.KEY:
        sys.exit("set ARIZE_API_KEY and ARIZE_SPACE_ID first")

    print("== Evaluators (Eval Hub) ==")
    evaluators = hub.ensure_evaluators()

    print("\n== Datasets ==")
    datasets = {}
    for skill in SKILLS:
        name, ds_id = hub.ensure_dataset(skill)
        datasets[name] = ds_id

    print("\n✓ Arize provisioned.")
    print(f"  evaluators: {evaluators}")
    print(f"  datasets:   {datasets}")


if __name__ == "__main__":
    main()
