"""Make gates/, experiments/ and harness/ importable from tests without a package install."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (REPO, REPO / "gates", REPO / "experiments", REPO / "harness"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
