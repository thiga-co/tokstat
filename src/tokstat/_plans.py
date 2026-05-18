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

# Plan capacity per rolling window. Both numbers are in *prompts*
# (i.e. distinct user inputs / exchanges) — the unit Anthropic uses
# in its public messaging.
PLANS: dict[str, dict[str, dict[str, int] | None]] = {
    "Claude Code": {
        "pro":    {"prompts_5h": 45,  "prompts_week": 315},
        "max5x":  {"prompts_5h": 225, "prompts_week": 1500},
        "max20x": {"prompts_5h": 900, "prompts_week": 6000},
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


def load_user_plans() -> dict[str, str]:
    """Read configured plans, with TOKSTAT_<TOOL>_PLAN overrides."""
    plans: dict[str, str] = {}
    if _CONFIG_PATH.exists():
        try:
            plans = json.loads(_CONFIG_PATH.read_text())
        except (OSError, json.JSONDecodeError):
            plans = {}
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
    when the plan is API-only (no fixed quota) or unknown."""
    if not plan_id:
        return None
    return PLANS.get(tool, {}).get(plan_id.lower())
