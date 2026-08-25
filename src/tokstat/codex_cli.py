#!/usr/bin/env python3
"""
codex-token-usage — Aggregate and display token consumption from OpenAI Codex.

Scans ~/.codex/sessions/ JSONL files to extract token usage data and estimates costs.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

from tokstat.cli import __version__

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tokstat._core import (
    BOLD, DIM, RESET, GREEN, YELLOW, RED, CYAN,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact, show_audit,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    print_retention_alerts,
)

# Register Codex color
TOOL_COLORS["Codex"] = GREEN


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_codex() -> list[dict]:
    """Scan Codex session JSONL files for token usage.

    Token data is in event_msg records with payload.type == "token_count".
    Project cwd comes from the session_meta record in the same file.
    """
    records = []
    base = Path.home() / ".codex" / "sessions"
    if not base.exists():
        return records
    for jsonl_file in base.rglob("*.jsonl"):
        project = "unknown"
        current_model = "codex-unknown"
        current_effort = ""
        try:
            with open(jsonl_file, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    payload = rec.get("payload", {})
                    if not isinstance(payload, dict):
                        continue
                    if rec.get("type") == "session_meta":
                        project = payload.get("cwd", "unknown")
                        continue
                    if rec.get("type") == "turn_context":
                        if payload.get("model"):
                            current_model = payload["model"]
                        current_effort = payload.get("effort", "")
                        continue
                    if rec.get("type") == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info") or {}
                        usage = info.get("last_token_usage") or info.get("total_token_usage") or {}
                        if not usage:
                            continue
                        ts_str = rec.get("timestamp", "")
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except (ValueError, AttributeError):
                            continue
                        # Codex's input_tokens is the FULL prompt and already
                        # includes cached_input_tokens; subtract so the cached
                        # portion isn't billed at the full input rate too.
                        cached = usage.get("cached_input_tokens", 0) or 0
                        raw_in = usage.get("input_tokens", 0) or 0
                        tokens = {
                            "input":       max(raw_in - cached, 0),
                            "output":      usage.get("output_tokens", 0),
                            "cache_read":  cached,
                            "cache_write": 0,
                        }
                        model = current_model
                        if current_effort and current_effort != "medium":
                            model = f"{model} [{current_effort}]"
                        records.append({
                            "tool":    "Codex",
                            "model":   model,
                            "project": project,
                            "ts":      ts,
                            **tokens,
                            "cost":    compute_cost(tokens, model),
                        })
        except (OSError, IOError):
            continue
    return records


def scan_speed_codex() -> list[dict]:
    """Extract output speed (tokens/sec) from Codex session JSONL files."""
    results = []
    base = Path.home() / ".codex" / "sessions"
    if not base.exists():
        return results
    for jsonl_file in base.rglob("*.jsonl"):
        try:
            events = []
            with open(jsonl_file, "r", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        events.append(json.loads(line))
                    except json.JSONDecodeError:
                        continue

            last_user_ts = None
            last_tc_ts = None
            current_model = "codex-unknown"
            current_effort = ""
            after_tool_call = False

            for rec in events:
                p = rec.get("payload") or {}
                if not isinstance(p, dict):
                    continue
                ts_str = rec.get("timestamp", "")
                rtype = rec.get("type", "")
                ptype = p.get("type", "")

                if rtype == "turn_context":
                    if p.get("model"):
                        current_model = p["model"]
                    current_effort = p.get("effort", "")
                    continue
                if ptype == "user_message":
                    try:
                        last_user_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        pass
                    last_tc_ts = None
                    after_tool_call = False
                    continue
                if ptype == "task_started":
                    last_tc_ts = None
                    after_tool_call = False
                    continue
                if ptype in ("function_call_output", "custom_tool_call_output"):
                    after_tool_call = True
                    continue
                if ptype == "token_count" and p.get("info"):
                    info = p["info"]
                    last_usage = info.get("last_token_usage") or {}
                    # output_tokens already includes reasoning_output_tokens.
                    out = last_usage.get("output_tokens", 0) or 0
                    if out < 10:
                        after_tool_call = False
                        continue
                    try:
                        tc_ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    if after_tool_call:
                        last_tc_ts = tc_ts
                        after_tool_call = False
                        continue
                    start = last_tc_ts or last_user_ts
                    if start is None:
                        continue
                    dt = (tc_ts - start).total_seconds()
                    if 0.3 < dt < 120:
                        speed = out / dt
                        if speed > 500:
                            last_tc_ts = tc_ts
                            continue
                        model = current_model
                        if current_effort and current_effort != "medium":
                            model = f"{model} [{current_effort}]"
                        results.append({
                            "tool":     "Codex",
                            "model":    model,
                            "ts":       tc_ts,
                            "tokens":   out,
                            "duration": dt,
                            "speed":    speed,
                            "ttft":     ((tc_ts - last_user_ts).total_seconds() - dt
                                         if last_tc_ts is None and last_user_ts else None),
                        })
                    last_tc_ts = tc_ts
                    after_tool_call = False
        except (OSError, IOError):
            continue
    return results


# Codex stores several synthetic messages with role=user that aren't real
# prompts: environment/context injections, shell-command echoes, AGENTS.md
# instructions, etc. They must not be counted as exchanges.
_CODEX_SYNTHETIC_PREFIXES = (
    "<environment_context>", "<user_shell_command>", "<user_instructions>",
    "<context>", "# AGENTS.md", "<editor_context>", "<attached_files>",
)


def _is_synthetic_user_text(text: str) -> bool:
    t = (text or "").lstrip()
    return any(t.startswith(p) for p in _CODEX_SYNTHETIC_PREFIXES)


def _extract_exchanges_codex(jsonl_path: str) -> list[dict]:
    """Parse a Codex rollout JSONL transcript into exchanges."""
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            lines = []
            for raw in f:
                raw = raw.strip()
                if raw:
                    try:
                        lines.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
    except (OSError, IOError):
        return []

    exchanges = []
    current = None
    current_model = None
    current_effort = ""
    current_cwd = None

    def _label(model, effort):
        """Same display name scan_codex uses, so token rows and prompt/turn
        rows land on a single model line."""
        m = model or "codex-unknown"
        if effort and effort != "medium":
            m = f"{m} [{effort}]"
        return m

    for rec in lines:
        rec_type = rec.get("type")
        payload = rec.get("payload", {})
        if not isinstance(payload, dict):
            continue
        ts_str = rec.get("timestamp")
        try:
            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else None
        except (ValueError, AttributeError):
            ts = None

        if rec_type == "session_meta":
            # cwd is established once per session here; turn_context may or
            # may not repeat it. Use it as the baseline project so exchanges
            # before the first turn_context aren't attributed to "unknown".
            if payload.get("cwd"):
                current_cwd = payload["cwd"]

        elif rec_type == "turn_context":
            if payload.get("model"):
                current_model = payload["model"]
            current_effort = payload.get("effort", "") or ""
            if payload.get("cwd"):
                current_cwd = payload["cwd"]
            # A real prompt can precede the first turn_context, so its model
            # was unknown at creation. Backfill it once the model is known.
            if (current is not None and current_model
                    and current["model"].startswith("codex-unknown")):
                current["model"] = _label(current_model, current_effort)

        elif rec_type == "response_item" and payload.get("role") == "user":
            text = ""
            for c in payload.get("content", []):
                if isinstance(c, dict) and c.get("type") == "input_text":
                    text = c.get("text", "").strip()
                    break
            # Skip Codex's synthetic context/shell/AGENTS injections — they
            # carry role=user but aren't real prompts. Tokens from later
            # turn_context turns still accrue to the current real exchange.
            if _is_synthetic_user_text(text):
                continue
            if current:
                exchanges.append(current)
            current = {
                "user_text": text, "assistant_texts": [], "tool_errors": [],
                "tools_used": {}, "num_turns": 0,
                "model": _label(current_model, current_effort),
                "project": current_cwd, "ts": ts,
                "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                "cost": 0.0,
            }

        elif rec_type == "response_item" and payload.get("role") == "assistant" and current:
            current["num_turns"] += 1
            for c in payload.get("content", []):
                if isinstance(c, dict) and c.get("type") == "output_text":
                    t = c.get("text", "").strip()
                    if t:
                        current["assistant_texts"].append(t)

        elif rec_type == "event_msg" and payload.get("type") == "token_count" and current:
            info = payload.get("info") or {}
            last = info.get("last_token_usage") or {}
            if last:
                cached  = last.get("cached_input_tokens", 0) or 0
                # input_tokens includes cached → subtract; output_tokens
                # already includes reasoning_output_tokens → don't re-add.
                inp     = max((last.get("input_tokens", 0) or 0) - cached, 0)
                out     = last.get("output_tokens", 0) or 0
                current["tokens"]["input"]      += inp
                current["tokens"]["output"]     += out
                current["tokens"]["cache_read"] += cached
                current["cost"] += compute_cost(
                    {"input": inp, "output": out, "cache_read": cached, "cache_write": 0},
                    current_model or "",
                )

    if current:
        exchanges.append(current)
    return exchanges


def _arg_value(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return default


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Collect Codex exchanges filtered by time."""
    all_exchanges = []
    tool_counts: dict[str, int] = {}

    def _add(tool_name, exchanges):
        if tool_filter and tool_name != tool_filter:
            return
        filtered = [ex for ex in exchanges
                    if ex["ts"] and ex["ts"] >= cutoff
                    and (cutoff_end is None or ex["ts"] < cutoff_end)]
        for ex in filtered:
            ex["tool"] = tool_name
        if filtered:
            all_exchanges.extend(filtered)
            tool_counts[tool_name] = tool_counts.get(tool_name, 0) + len(filtered)

    codex_base = Path.home() / ".codex" / "sessions"
    if codex_base.exists():
        for jsonl_file in codex_base.rglob("rollout-*.jsonl"):
            _add("Codex", _extract_exchanges_codex(str(jsonl_file)))

    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — Codex{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.codex/sessions/...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    data_path = "~/.codex/"
    expanded = Path(data_path.replace("~", str(Path.home())))
    if not expanded.exists():
        print(f"  {DIM}Codex not found at {data_path}{RESET}\n")
        return

    records = scan_codex()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {GREEN}●{RESET} {'Codex':<12} {len(records):>6} records from {data_path}")
    print()
    print_retention_alerts(["Codex"])
    print(f"  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    speed_records = scan_speed_codex()
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {
    "codex": "Codex", "openai": "Codex",
}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--activity", "--total", "--impact", "--audit", "--judge", "--model", "--judge-max", "--export", "--period", "--since", "--tool",
}


def _parse_tool(args: list[str]) -> str | None:
    if "--tool" not in args:
        return None
    idx = args.index("--tool")
    if idx + 1 >= len(args):
        return None
    raw = args[idx + 1].lower().strip()
    if raw in ("all", "tous", "*"):
        return None
    canonical = _TOOL_ALIASES.get(raw)
    if canonical:
        return canonical
    for alias, name in _TOOL_ALIASES.items():
        if raw in alias or raw in name.lower():
            return name
    valid = ", ".join(sorted(set(_TOOL_ALIASES.values())))
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: {valid}")


def show_help():
    print(f"""
{BOLD}codex-token-usage{RESET} — Aggregate and analyze Codex (OpenAI) token consumption.

{BOLD}MODES{RESET}
  codex-token-usage                            Aggregated overview (period, project, model, speed)
  codex-token-usage --prompts  [-p]            Per-exchange detail (text, model, turns, tokens)
  codex-token-usage --anomalies                Technical anomaly detection (cost, cache, tool storms)
  codex-token-usage --activity                 Activity calendar (GitHub-style, by day)
  codex-token-usage --total                    Compact totals (tokens + cost + data span)
  codex-token-usage --impact                   Energy & CO₂ estimate (EcoLogits)
  codex-token-usage --plan                     Cost breakdown + plan recommendation + optimization tips
  codex-token-usage --export   [file.json]     Export all exchanges to JSON
  codex-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}  {DIM}(apply to all modes){RESET}
  --period <period>      Time filter — all, hour, "5 hours", today, yesterday, "7 days", "30 days", "3 months", "6 months", year

  Default period: today. Partial match works ("7" → "Last 7 days").

{BOLD}DATA SOURCE{RESET}
  {GREEN}Codex{RESET}    {DIM}~/.codex/sessions/{RESET}    ✓ Tokens ✓ Text ✓ Speed

{BOLD}QUICK START{RESET}
  codex-token-usage                              # Full overview, today
  codex-token-usage --period all                 # All time

{BOLD}PER-EXCHANGE DETAIL{RESET}
  codex-token-usage --prompts                    # Today's exchanges
  codex-token-usage -p --period "7 days"         # Last 7 days

{BOLD}ANALYSIS MODES{RESET}
  codex-token-usage --anomalies                  # Technical anomalies (high cost, tool storms)
  codex-token-usage --anomalies --period "30 days"
  codex-token-usage --plan                       # Cost breakdown + plan recommendation
  codex-token-usage --plan --period all          # Projection based on all-time usage

{BOLD}EXPORT{RESET}
  codex-token-usage --export                     # Save to conversations.json
  codex-token-usage --export out.json --period "7 days"
""")


def cli():
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print(f"tokstat {__version__}")
        return
    if "--help" in args or "-h" in args:
        show_help()
        return

    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"\n  {RED}Unknown option(s): {', '.join(unknown)}{RESET}")
        print(f"  Run {BOLD}codex-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)
    try:
        tool = _parse_tool(args)
    except ValueError as e:
        print(f"\n  {RED}{e}{RESET}\n")
        sys.exit(1)

    if "--prompts" in args or "-p" in args:
        show_prompts(_collect_all_exchanges, period, tool)
    elif "--anomalies" in args:
        show_anomalies(_collect_all_exchanges, period, tool)
    elif "--activity" in args:
        show_activity(_collect_all_exchanges, period, tool)
    elif "--total" in args:
        show_total(_collect_all_exchanges, period, tool)
    elif "--impact" in args:
        show_impact(_collect_all_exchanges, period, tool, _parse_region(args))
    elif "--audit" in args:
        jmax_raw = _arg_value(args, "--judge-max")
        try:
            jmax = int(jmax_raw) if jmax_raw is not None else None
        except ValueError:
            jmax = None
        show_audit(_collect_all_exchanges, period, tool,
                   judge_model=_arg_value(args, "--model"), judge_max=jmax)
    elif "--plan" in args:
        show_plan(_collect_all_exchanges, period, tool)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(_collect_all_exchanges, out, period, tool)
    else:
        main(period, tool)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
