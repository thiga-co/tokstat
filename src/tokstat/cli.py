#!/usr/bin/env python3
"""
claude-token-usage — Aggregate and display token consumption from Claude Code.

Scans ~/.claude/projects/ JSONL transcripts to extract token usage data and estimates costs.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

__version__ = "1.4.0"

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tokstat._core import (
    BOLD, DIM, RESET, CYAN, GREEN, YELLOW, RED,
    BRED, BYELLOW,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period, period_boundaries, classify_periods,
    normalize_project, _warm_worktree_cache,
    fmt_tokens, fmt_cost, calc_table_width, print_table, shorten_path,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    export_conversations, _parse_period, print_update_notice,
)

# Register Claude Code color
TOOL_COLORS["Claude Code"] = CYAN


# ─── Scanners ────────────────────────────────────────────────────────────────

def decode_project_dir(dirname: str) -> str:
    """Convert Claude Code project dir name back to a path."""
    PLACEHOLDER = "\x00DASH\x00"
    return dirname.replace("---", PLACEHOLDER).replace("-", "/").replace(PLACEHOLDER, "-")


def scan_claude_code() -> list[dict]:
    """Scan Claude Code JSONL transcripts for token usage."""
    records = []
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return records
    for proj_dir in base.iterdir():
        if not proj_dir.is_dir():
            continue
        project = decode_project_dir(proj_dir.name)
        for jsonl_file in proj_dir.glob("*.jsonl"):
            try:
                prev_msg_id = None
                pending = None
                with open(jsonl_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = rec.get("message", {})
                        if not isinstance(msg, dict):
                            continue
                        usage = msg.get("usage")
                        if not usage:
                            continue
                        ts_str = rec.get("timestamp")
                        if not ts_str:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except (ValueError, AttributeError):
                            continue
                        msg_id = msg.get("id", "")
                        model = msg.get("model", "unknown")
                        speed_mode = usage.get("speed", "")
                        if speed_mode and speed_mode != "standard":
                            model = f"{model} [{speed_mode}]"
                        rec_project = rec.get("cwd") or project
                        entry = {
                            "tool":        "Claude Code",
                            "model":       model,
                            "project":     rec_project,
                            "ts":          ts,
                            "input":       usage.get("input_tokens", 0),
                            "output":      usage.get("output_tokens", 0),
                            "cache_read":  usage.get("cache_read_input_tokens", 0),
                            "cache_write": usage.get("cache_creation_input_tokens", 0),
                        }
                        if msg_id and msg_id == prev_msg_id:
                            pending = entry
                        else:
                            if pending:
                                pending["cost"] = compute_cost(pending, pending["model"])
                                records.append(pending)
                            pending = entry
                        prev_msg_id = msg_id
                if pending:
                    pending["cost"] = compute_cost(pending, pending["model"])
                    records.append(pending)
            except (OSError, IOError):
                continue
    return records


def scan_speed_claude_code() -> list[dict]:
    """Extract output speed (tokens/sec) from Claude Code JSONL transcripts."""
    results = []
    base = Path.home() / ".claude" / "projects"
    if not base.exists():
        return results
    for proj_dir in base.iterdir():
        if not proj_dir.is_dir():
            continue
        for jsonl_file in proj_dir.glob("*.jsonl"):
            try:
                msgs = []
                with open(jsonl_file, "r", errors="replace") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            rec = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        msg = rec.get("message", {})
                        if not isinstance(msg, dict) or not msg.get("role"):
                            continue
                        ts_str = rec.get("timestamp")
                        if not ts_str:
                            continue
                        try:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                        except (ValueError, AttributeError):
                            continue
                        usage = msg.get("usage") or {}
                        model = msg.get("model", "unknown")
                        speed_mode = usage.get("speed", "")
                        if speed_mode and speed_mode != "standard":
                            model = f"{model} [{speed_mode}]"
                        msgs.append({
                            "role":   msg["role"],
                            "ts":     ts,
                            "output": usage.get("output_tokens", 0),
                            "model":  model,
                        })
                for i, m in enumerate(msgs):
                    if m["role"] != "user":
                        continue
                    assistants = []
                    for j in range(i + 1, len(msgs)):
                        if msgs[j]["role"] == "assistant":
                            assistants.append(msgs[j])
                        else:
                            break
                    if not assistants:
                        continue
                    last = assistants[-1]
                    if last["output"] < 10:
                        continue
                    if len(assistants) >= 2:
                        dt = (last["ts"] - assistants[0]["ts"]).total_seconds()
                        ttft = (assistants[0]["ts"] - m["ts"]).total_seconds()
                    else:
                        dt = (last["ts"] - m["ts"]).total_seconds()
                        ttft = dt
                    if dt < 0.5 or dt > 300:
                        continue
                    results.append({
                        "tool":     "Claude Code",
                        "model":    last["model"],
                        "ts":       last["ts"],
                        "tokens":   last["output"],
                        "duration": dt,
                        "speed":    last["output"] / dt,
                        "ttft":     ttft if len(assistants) >= 2 else None,
                    })
            except (OSError, IOError):
                continue
    return results


def _extract_exchanges(jsonl_path: str) -> list[dict]:
    """Parse a Claude Code JSONL transcript into exchanges."""
    try:
        with open(jsonl_path, "r", errors="replace") as f:
            lines = [json.loads(raw.strip()) for raw in f
                     if raw.strip() and _safe_json(raw.strip())]
    except (OSError, IOError):
        return []

    exchanges = []
    current = None

    for rec in lines:
        rec_type = rec.get("type")
        msg = rec.get("message", {})
        if not isinstance(msg, dict):
            msg = {}
        content = msg.get("content", "")

        if rec_type == "user":
            is_tool_result = (isinstance(content, list) and
                              any(isinstance(c, dict) and c.get("type") == "tool_result"
                                  for c in content))
            if not is_tool_result:
                if current:
                    exchanges.append(current)
                text = ""
                if isinstance(content, str):
                    text = content.strip()
                elif isinstance(content, list):
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            text = c.get("text", "").strip()
                            break
                ts_str = rec.get("timestamp")
                try:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00")) if ts_str else None
                except (ValueError, AttributeError):
                    ts = None
                current = {
                    "user_text": text, "assistant_texts": [], "tool_errors": [],
                    "tools_used": defaultdict(int), "num_turns": 0, "model": None,
                    "project": rec.get("cwd"), "ts": ts,
                    "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                    "cost": 0.0,
                }
            elif current and isinstance(content, list):
                for c in content:
                    if isinstance(c, dict) and c.get("type") == "tool_result" and c.get("is_error"):
                        err = c.get("content", "")
                        if isinstance(err, list):
                            err = " ".join(str(e) for e in err)
                        current["tool_errors"].append(str(err)[:200])

        elif rec_type == "assistant" and current is not None:
            if not current["model"]:
                current["model"] = msg.get("model")
            usage = msg.get("usage")
            if usage:
                msg_id = msg.get("id", "")
                prev_id = current.get("_prev_msg_id")
                if msg_id and msg_id == prev_id:
                    current["tokens"]["input"]       = current["_prev_tokens"]["input"]       + usage.get("input_tokens", 0)
                    current["tokens"]["output"]      = current["_prev_tokens"]["output"]      + usage.get("output_tokens", 0)
                    current["tokens"]["cache_read"]  = current["_prev_tokens"]["cache_read"]  + usage.get("cache_read_input_tokens", 0)
                    current["tokens"]["cache_write"] = current["_prev_tokens"]["cache_write"] + usage.get("cache_creation_input_tokens", 0)
                    current["cost"] = current["_prev_cost"] + compute_cost({
                        "input":       usage.get("input_tokens", 0),
                        "output":      usage.get("output_tokens", 0),
                        "cache_read":  usage.get("cache_read_input_tokens", 0),
                        "cache_write": usage.get("cache_creation_input_tokens", 0),
                    }, msg.get("model", ""))
                else:
                    current["_prev_msg_id"] = msg_id
                    current["_prev_tokens"] = dict(current["tokens"])
                    current["_prev_cost"] = current["cost"]
                    current["num_turns"] += 1
                    current["tokens"]["input"]       += usage.get("input_tokens", 0)
                    current["tokens"]["output"]      += usage.get("output_tokens", 0)
                    current["tokens"]["cache_read"]  += usage.get("cache_read_input_tokens", 0)
                    current["tokens"]["cache_write"] += usage.get("cache_creation_input_tokens", 0)
                    current["cost"] += compute_cost({
                        "input":       usage.get("input_tokens", 0),
                        "output":      usage.get("output_tokens", 0),
                        "cache_read":  usage.get("cache_read_input_tokens", 0),
                        "cache_write": usage.get("cache_creation_input_tokens", 0),
                    }, msg.get("model", ""))
            if isinstance(content, list):
                for c in content:
                    if isinstance(c, dict):
                        if c.get("type") == "text":
                            t = c.get("text", "").strip()
                            if t:
                                current["assistant_texts"].append(t)
                        elif c.get("type") == "tool_use":
                            current["tools_used"][c.get("name", "unknown")] += 1

    if current:
        exchanges.append(current)
    for ex in exchanges:
        ex.pop("_prev_msg_id", None)
        ex.pop("_prev_tokens", None)
        ex.pop("_prev_cost", None)
    return exchanges


def _safe_json(s):
    try:
        import json as _j
        _j.loads(s)
        return True
    except Exception:
        return False


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Collect Claude Code exchanges filtered by time."""
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

    base = Path.home() / ".claude" / "projects"
    if base.exists():
        for proj_dir in base.iterdir():
            if not proj_dir.is_dir():
                continue
            for jsonl_file in proj_dir.glob("*.jsonl"):
                if "/subagents/" in str(jsonl_file):
                    continue
                _add("Claude Code", _extract_exchanges(str(jsonl_file)))

    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — Claude Code{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.claude/projects/...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    records = scan_claude_code()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    data_path = "~/.claude/"
    expanded = Path(data_path.replace("~", str(Path.home())))
    if not expanded.exists():
        print(f"  {DIM}Claude Code not found at {data_path}{RESET}\n")
        return

    if records:
        print(f"  {CYAN}●{RESET} {'Claude Code':<12} {len(records):>6} records from {data_path}")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    speed_records = scan_speed_claude_code()
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]

    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label, tool_filter)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {
    "claude": "Claude Code", "claude-code": "Claude Code", "claudecode": "Claude Code",
}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--export", "--period", "--since", "--tool",
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
{BOLD}claude-token-usage{RESET} — Aggregate and analyze Claude Code token consumption.

{BOLD}MODES{RESET}
  claude-token-usage                            Aggregated overview (period, project, model, speed)
  claude-token-usage --prompts  [-p]            Per-exchange detail (text, model, turns, tokens, tools)
  claude-token-usage --anomalies                Technical anomaly detection (cost, cache, tool storms)
  claude-token-usage --plan                     Cost breakdown + plan recommendation + optimization tips
  claude-token-usage --export   [file.json]     Export all exchanges to JSON
  claude-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}  {DIM}(apply to all modes){RESET}
  --period <period>      Time filter — all, hour, "5 hours", today, yesterday, "7 days", "30 days", year

  Default period: today. Partial match works ("7" → "Last 7 days").

{BOLD}DATA SOURCE{RESET}
  {CYAN}Claude Code{RESET}    {DIM}~/.claude/projects/{RESET}    ✓ Tokens ✓ Text ✓ Tools ✓ Speed

{BOLD}QUICK START{RESET}
  claude-token-usage                              # Full overview, today
  claude-token-usage --period all                 # All time

{BOLD}PER-EXCHANGE DETAIL{RESET}
  claude-token-usage --prompts                    # Today's exchanges
  claude-token-usage -p --period "7 days"         # Last 7 days

{BOLD}ANALYSIS MODES{RESET}
  claude-token-usage --anomalies                  # Technical anomalies (high cost, tool storms)
  claude-token-usage --anomalies --period "30 days"
  claude-token-usage --plan                       # Cost breakdown + plan recommendation
  claude-token-usage --plan --period all          # Projection based on all-time usage

{BOLD}EXPORT{RESET}
  claude-token-usage --export                     # Save to conversations.json
  claude-token-usage --export out.json --period "7 days"
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
        print(f"  Run {BOLD}claude-token-usage --help{RESET} for usage.\n")
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
