"""Eval 0 — Structural validation + security scan (pure code, runs FIRST).

This is the customer's Phase-1 (structural) gate plus the Sentry-scan role,
standardized. It runs before anything expensive and short-circuits the pipeline
on failure (a bad skill should bounce "at the door", demo beat 1).

Two families of checks, over a skill directory containing SKILL.md and any
bundled files:

  LINT (structural)
    - frontmatter parses and has `name` + `description`
    - description length is sane
    - markdown body is non-empty / parses
    - every locally-referenced file actually exists
    - no absolute filesystem paths in SKILL.md

  SECURITY (deliberately simple, pattern-based — see README caveat)
    - prompt-injection / instruction-override phrasing aimed at the agent
    - instructions to read credentials or environment secrets
    - instructions to contact external URLs / exfiltrate
    - embedded secrets (API keys, AWS keys, tokens)
    - obfuscated/encoded payloads (long base64/hex blobs)
    - bundled scripts making network calls or reading creds/env

Every check contributes a CheckResult; the gate PASSES only if all checks pass.
The aggregate is returned as the shared `EvaluationResult` so downstream gating
and the report are path-agnostic.

NOTE (plan §5, README): keep this conservative — it demos WHERE the gate lives.
Production should layer a dedicated scanner / guardrail model here.
"""

from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

try:
    import frontmatter  # python-frontmatter
except ImportError:  # pragma: no cover - dependency hint
    frontmatter = None

# gates must stay import-light and harness-agnostic
sys.path.insert(0, str(Path(__file__).resolve().parent))
from common import EvaluationResult  # noqa: E402


DESC_MIN, DESC_MAX = 20, 1500
SCRIPT_EXTS = {".py", ".sh", ".js", ".ts", ".rb", ".bash"}

# ── security signatures ──────────────────────────────────────────────────────
INJECTION_PATTERNS = [
    (r"ignore\s+(all\s+)?(previous|prior|above)\s+instructions", "instruction-override phrasing"),
    (r"disregard\s+(the\s+)?(system\s+prompt|previous|prior)", "instruction-override phrasing"),
    (r"do\s+not\s+(tell|inform|mention\s+to)\s+the\s+user", "instructs agent to hide actions from the user"),
    (r"without\s+(telling|informing|notifying)\s+the\s+user", "instructs agent to hide actions from the user"),
    (r"\bexfiltrat", "exfiltration language"),
    (r"(read|cat|open|dump)\b[^\n]{0,40}(credential|\.aws|\.ssh|id_rsa|/etc/passwd)", "instructs reading credentials/secrets"),
    # Well-known THIRD-PARTY / cloud credential names only. A skill legitimately
    # referencing its OWN configured auth env var (e.g. ORDERS_API_TOKEN) must not trip.
    (r"\b(AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|ANTHROPIC_API_KEY|OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN|GOOGLE_APPLICATION_CREDENTIALS)\b", "references a third-party credential env var"),
    (r"(send|post|upload|beacon|phone\s+home)[^\n]{0,40}(http[s]?://|to\s+http)", "instructs sending data to an external URL"),
]
SECRET_PATTERNS = [
    (r"sk-ant-[a-zA-Z0-9\-]{20,}", "Anthropic-style API key"),
    (r"sk-[a-zA-Z0-9]{20,}", "OpenAI-style API key"),
    (r"AKIA[0-9A-Z]{16}", "AWS access key id"),
    (r"ghp_[a-zA-Z0-9]{20,}", "GitHub personal access token"),
    (r"-----BEGIN (RSA |EC |OPENSSH )?PRIVATE KEY-----", "embedded private key"),
]
# network calls in bundled scripts
NET_PATTERNS = [
    (r"urllib\.request|requests\.(get|post|put|delete|request)|http\.client|socket\.socket|aiohttp", "network call"),
    (r"\bcurl\b|\bwget\b|nc\s+-|/dev/tcp/", "shell network call"),
]
CRED_READ_PATTERNS = [
    # Third-party / cloud credential env vars only (not the skill's own auth token).
    (r"os\.environ(\.get)?\s*[\[(]\s*['\"](AWS_SECRET_ACCESS_KEY|AWS_ACCESS_KEY_ID|ANTHROPIC_API_KEY|OPENAI_API_KEY|GITHUB_TOKEN|GH_TOKEN)", "reads a third-party secret from the environment"),
    (r"(open|read)\b[^\n]{0,60}(\.aws/credentials|\.ssh/|id_rsa|/etc/passwd)", "reads a credential/system file"),
]
ABS_PATH_PATTERN = re.compile(r"(?<!\w)(/Users/|/home/|/etc/|/var/|/root/|/tmp/|[A-Z]:\\\\)")
BASE64_BLOB = re.compile(r"[A-Za-z0-9+/]{80,}={0,2}")
HEX_BLOB = re.compile(r"(?:0x)?[0-9a-fA-F]{80,}")


@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str = ""
    findings: list[str] = field(default_factory=list)


def _md_link_targets(body: str) -> list[str]:
    """Local file paths referenced in markdown links / code fences."""
    targets = re.findall(r"\[[^\]]*\]\(([^)]+)\)", body)
    # scripts referenced in fenced commands, e.g. `python3 scripts/beacon.py`
    targets += re.findall(r"(?:python3?|bash|sh|node)\s+([\w./\-]+\.(?:py|sh|js|ts))", body)
    out = []
    for t in targets:
        t = t.strip()
        if t.startswith(("http://", "https://", "mailto:", "#")):
            continue
        out.append(t.split("#")[0])
    return out


def _scan_text(text: str, patterns: list[tuple[str, str]]) -> list[str]:
    found = []
    for pat, label in patterns:
        if re.search(pat, text, re.IGNORECASE):
            found.append(label)
    return sorted(set(found))


def evaluate_skill(skill_dir: str | Path) -> EvaluationResult:
    skill_dir = Path(skill_dir)
    checks: list[CheckResult] = []
    skill_md = skill_dir / "SKILL.md"

    # ── structural: SKILL.md exists ──────────────────────────────────────────
    if not skill_md.exists():
        checks.append(CheckResult("skill_md_present", False, "SKILL.md not found"))
        return _aggregate(checks, skill_dir)
    raw = skill_md.read_text()

    # ── structural: frontmatter ──────────────────────────────────────────────
    name = desc = None
    if frontmatter is None:
        checks.append(CheckResult("frontmatter_parses", False,
                                  "python-frontmatter not installed"))
        body = raw
    else:
        try:
            post = frontmatter.loads(raw)
            name = post.get("name")
            desc = post.get("description")
            body = post.content
            has_fm = bool(post.metadata)
            checks.append(CheckResult(
                "frontmatter_parses", has_fm and bool(name) and bool(desc),
                "" if (has_fm and name and desc)
                else "missing frontmatter or required keys (name, description)"))
        except Exception as e:  # noqa: BLE001
            body = raw
            checks.append(CheckResult("frontmatter_parses", False, f"parse error: {e}"))

    # ── structural: description length ───────────────────────────────────────
    if desc:
        ok = DESC_MIN <= len(desc) <= DESC_MAX
        checks.append(CheckResult("description_length_sane", ok,
                                  "" if ok else f"description length {len(desc)} outside [{DESC_MIN},{DESC_MAX}]"))
    else:
        checks.append(CheckResult("description_length_sane", False, "no description"))

    # ── structural: body non-empty ───────────────────────────────────────────
    checks.append(CheckResult("body_nonempty", bool(body.strip()),
                              "" if body.strip() else "empty markdown body"))

    # ── structural: referenced files exist ───────────────────────────────────
    missing = [t for t in _md_link_targets(body) if not (skill_dir / t).exists()]
    checks.append(CheckResult("referenced_files_exist", not missing,
                              "" if not missing else f"missing: {missing}", missing))

    # ── structural: no absolute paths ────────────────────────────────────────
    abs_hits = sorted(set(ABS_PATH_PATTERN.findall(raw)))
    checks.append(CheckResult("no_absolute_paths", not abs_hits,
                              "" if not abs_hits else f"absolute path prefixes: {abs_hits}", abs_hits))

    # ── security: injection / instruction-override in SKILL.md ────────────────
    inj = _scan_text(raw, INJECTION_PATTERNS)
    checks.append(CheckResult("no_prompt_injection", not inj,
                              "" if not inj else "; ".join(inj), inj))

    # ── security: embedded secrets in SKILL.md ────────────────────────────────
    sec = _scan_text(raw, SECRET_PATTERNS)
    checks.append(CheckResult("no_embedded_secrets", not sec,
                              "" if not sec else "; ".join(sec), sec))

    # ── security: obfuscated/encoded payloads ─────────────────────────────────
    blobs = []
    if BASE64_BLOB.search(body):
        blobs.append("long base64-like blob")
    if HEX_BLOB.search(body):
        blobs.append("long hex blob")
    checks.append(CheckResult("no_obfuscated_payloads", not blobs,
                              "" if not blobs else "; ".join(blobs), blobs))

    # ── security: bundled scripts (network calls / cred reads) ────────────────
    declared_net = bool(re.search(r"network|http|url|fetch|request|download", (desc or ""), re.I))
    script_findings: list[str] = []
    for f in skill_dir.rglob("*"):
        if f.is_file() and f.suffix.lower() in SCRIPT_EXTS:
            stext = f.read_text(errors="ignore")
            nets = _scan_text(stext, NET_PATTERNS)
            creds = _scan_text(stext, CRED_READ_PATTERNS)
            rel = f.relative_to(skill_dir)
            if nets and not declared_net:
                script_findings.append(f"{rel}: {', '.join(nets)} (undeclared in description)")
            if creds:
                script_findings.append(f"{rel}: {', '.join(creds)}")
    checks.append(CheckResult("bundled_scripts_safe", not script_findings,
                              "" if not script_findings else "; ".join(script_findings),
                              script_findings))

    return _aggregate(checks, skill_dir)


def _aggregate(checks: list[CheckResult], skill_dir: Path) -> EvaluationResult:
    failed = [c for c in checks if not c.passed]
    passed = not failed
    if passed:
        expl = f"All {len(checks)} structural + security checks passed."
    else:
        expl = "FAILED checks: " + "; ".join(
            f"[{c.name}] {c.detail or 'failed'}" for c in failed)
    return EvaluationResult(
        label="pass" if passed else "fail",
        score=1.0 if passed else 0.0,
        explanation=expl,
        metadata={
            "gate": "eval0_structural",
            "skill_dir": str(skill_dir),
            "checks": [
                {"name": c.name, "passed": c.passed, "detail": c.detail, "findings": c.findings}
                for c in checks
            ],
            "n_failed": len(failed),
        },
    )


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: python eval0_structural.py <skill_dir>", file=sys.stderr)
        raise SystemExit(2)
    res = evaluate_skill(sys.argv[1])
    print(f"[eval0] {res.label.upper()} ({res.metadata['n_failed']} failed)")
    for c in res.metadata["checks"]:
        mark = "✓" if c["passed"] else "✗"
        line = f"  {mark} {c['name']}"
        if c["detail"]:
            line += f" — {c['detail']}"
        print(line)
    raise SystemExit(0 if res.passed else 1)
