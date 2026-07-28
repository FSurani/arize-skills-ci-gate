"""Make gates/ and verifiers importable from tests without installing a package."""
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (REPO, REPO / "gates", REPO / "verifiers" / "api_helper"):
    sp = str(p)
    if sp not in sys.path:
        sys.path.insert(0, sp)
