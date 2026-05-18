"""Subscription plan limits and config loading for tokstat.

Limits are best-effort estimates derived from publicly stated quotas
(Anthropic Pro / Max plans, OpenAI ChatGPT Plus / Pro). Numbers are
approximate — Anthropic in particular publishes "messages per 5 hours"
which varies by message length. Override by editing the file at
~/.config/tokstat/plans.json or by exporting TOKSTAT_<TOOL>_PLAN.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

# Plan capacity per rolling window. Numbers are in *prompts* (distinct
# user inputs / exchanges). Calibrated against live Claude Code session
# meters — Claude Code's effective cap is much higher than Anthropic's
# public chat.anthropic.com "messages per 5h" numbers; the values here
# match what the CLI itself reports as remaining.
# Override per-tool by writing under "_custom" in ~/.config/tokstat/plans.json:
#   {"Claude Code": "pro", "_custom": {"Claude Code": {"prompts_5h": 300}}}
PLANS: dict[str, dict[str, dict[str, int] | None]] = {
    "Claude Code": {
        "pro":    {"prompts_5h": 275,  "prompts_week": 1800},
        "max5x":  {"prompts_5h": 1375, "prompts_week": 9000},
        "max20x": {"prompts_5h": 5500, "prompts_week": 36000},
        "api":    None,
    },
    "Codex": {
        "plus":     {"prompts_5h": 130, "prompts_week": 900},
        "pro":      {"prompts_5h": 9999, "prompts_week": 999999},
        "business": {"prompts_5h": 9999, "prompts_week": 999999},
        "api":      None,
    },
}

PLAN_LABELS = {
    "pro":    "Pro",
    "max5x":  "Max 5x",
    "max20x": "Max 20x",
    "plus":   "Plus",
    "business": "Business",
    "api":    "API",
}

_CONFIG_PATH = Path.home() / ".config" / "tokstat" / "plans.json"


def _load_config() -> dict:
    if not _CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(_CONFIG_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return {}


def load_user_plans() -> dict[str, str]:
    """Read configured plans, with TOKSTAT_<TOOL>_PLAN overrides."""
    plans = {k: v for k, v in _load_config().items()
             if isinstance(v, str)}
    env_overrides = {
        "Claude Code": os.environ.get("TOKSTAT_CLAUDE_PLAN"),
        "Codex":       os.environ.get("TOKSTAT_CODEX_PLAN"),
    }
    for tool, env_val in env_overrides.items():
        if env_val:
            plans[tool] = env_val.lower()
    return plans


def plan_limits_for(tool: str, plan_id: str | None) -> dict[str, int] | None:
    """Return {prompts_5h, prompts_week} for the given tool/plan, or None
    when the plan is API-only (no fixed quota) or unknown. User-supplied
    overrides in plans.json -> _custom -> <tool> are merged on top of the
    plan defaults.
    """
    if not plan_id:
        return None
    base = PLANS.get(tool, {}).get(plan_id.lower())
    if base is None:
        return None
    custom = (_load_config().get("_custom") or {}).get(tool, {})
    if isinstance(custom, dict) and custom:
        return {**base, **{k: v for k, v in custom.items()
                           if isinstance(v, (int, float))}}
    return base
