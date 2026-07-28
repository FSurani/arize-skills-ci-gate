"""AX tracing wiring for the harness.

Primary path: the **Coding Harness Tracing plugin** instruments
Claude Code via settings.json hooks (SessionStart/PreToolUse/PostToolUse/…),
which fire for both the `claude` CLI and the Agent SDK because the SDK reads the
same settings. This module writes the sandbox's project-local
`.claude/settings.local.json` with the AX `env` block the plugin reads, plus a
permission denylist so headless bypass stays safe.

Supplementary path: `ax_tracer_provider()` registers a direct OpenInference→AX
OTLP exporter via `arize-otel`, so run_case.py can emit a normalized agent/tool
span tree to AX even when the plugin is not installed. The plugin remains the
recommended source of truth.

Nothing here spends tokens; `ARIZE_DRY_RUN=true` builds spans without shipping.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Shell patterns blocked even under bypass-permissions in the sandbox.
DEFAULT_DENYLIST = [
    "Bash(rm:*)",
    "Bash(sudo:*)",
    "Bash(curl:*)",
    "Bash(wget:*)",
    "Bash(ssh:*)",
    "Bash(scp:*)",
    "Bash(:*/dev/tcp/*)",
    "Bash(git push:*)",
    "WebFetch",
    "WebSearch",
]


def write_sandbox_settings(
    sandbox: str | Path,
    *,
    project_name: str = "skills-eval",
    dry_run: bool | None = None,
    denylist: list[str] | None = None,
    plugin_hooks: dict | None = None,
) -> Path:
    """Write `.claude/settings.local.json` into the sandbox.

    Reads ARIZE_API_KEY / ARIZE_SPACE_ID from the environment (never hardcoded).
    The `env` keys match the coding-harness-tracing plugin's documented block.
    """
    sandbox = Path(sandbox)
    settings_dir = sandbox / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)

    dry = os.environ.get("ARIZE_DRY_RUN", "false") if dry_run is None else ("true" if dry_run else "false")
    env = {
        "ARIZE_PROJECT_NAME": project_name,
        "ARIZE_API_KEY": os.environ.get("ARIZE_API_KEY", ""),
        "ARIZE_SPACE_ID": os.environ.get("ARIZE_SPACE_ID", ""),
        "ARIZE_LOG_PROMPTS": "true",
        "ARIZE_LOG_TOOL_DETAILS": "true",
        "ARIZE_LOG_TOOL_CONTENT": "true",
        "ARIZE_DRY_RUN": dry,
    }
    settings: dict = {
        "env": env,
        "permissions": {
            "deny": denylist if denylist is not None else DEFAULT_DENYLIST,
        },
    }
    if plugin_hooks:
        settings["hooks"] = plugin_hooks

    path = settings_dir / "settings.local.json"
    path.write_text(json.dumps(settings, indent=2))
    return path


def credentials_present() -> bool:
    return bool(os.environ.get("ARIZE_API_KEY") and os.environ.get("ARIZE_SPACE_ID"))


def plugin_dir() -> Path | None:
    """Local path of the installed coding-harness-tracing Claude Code plugin, or
    None if not installed. Passed to ClaudeAgentOptions(plugins=[...]) so the SDK
    loads it and its hooks fire (the SDK needs the explicit local-plugin entry —
    see the plugin's own agent_sdk.claude_options helper)."""
    p = Path.home() / ".claude/plugins/marketplaces/coding-harness-tracing/tracing/claude_code"
    return p if (p / ".claude-plugin" / "plugin.json").exists() else None


def ax_env(project_name: str = "skills-eval") -> dict[str, str]:
    """The ARIZE_* env the tracing plugin reads. ARIZE_SPACE_ID stays in its
    base64/console form here — that is what OTLP tracing expects (unlike the CLI,
    which wants the decoded form for its REST calls)."""
    return {
        "ARIZE_PROJECT_NAME": project_name,
        "ARIZE_API_KEY": os.environ.get("ARIZE_API_KEY", ""),
        "ARIZE_SPACE_ID": os.environ.get("ARIZE_SPACE_ID", ""),
        "ARIZE_LOG_PROMPTS": "true",
        "ARIZE_LOG_TOOL_DETAILS": "true",
        "ARIZE_LOG_TOOL_CONTENT": "true",
        "ARIZE_DRY_RUN": os.environ.get("ARIZE_DRY_RUN", "false"),
    }


def ax_tracer_provider(project_name: str = "skills-eval"):
    """Supplementary direct OTLP→AX exporter (OpenInference conventions).

    Returns a configured tracer provider, or None if creds/deps are missing.
    Honors ARIZE_DRY_RUN by skipping registration.
    """
    if os.environ.get("ARIZE_DRY_RUN", "false").lower() == "true":
        return None
    if not credentials_present():
        return None
    try:
        from arize.otel import register
    except ImportError:
        return None
    return register(
        space_id=os.environ["ARIZE_SPACE_ID"],
        api_key=os.environ["ARIZE_API_KEY"],
        project_name=project_name,
    )


# ── token → cost helper (Sonnet-class harness model) ─────────────────────────
_PRICE_IN, _PRICE_OUT = 3.0e-6, 15.0e-6  # USD/token


def est_cost_usd(tokens_input: int, tokens_output: int) -> float:
    return tokens_input * _PRICE_IN + tokens_output * _PRICE_OUT
