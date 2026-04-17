#!/usr/bin/env python3
"""
gemini-token-usage — Aggregate and display token consumption from Gemini CLI.

Data source: ~/.gemini/tmp/{project_hash}/chats/session-*.json
Token data is stored directly in gemini messages (input/output/cached/thoughts).

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tokstat.cli import __version__
from tokstat._core import (
    BOLD, DIM, RESET, YELLOW, RED, GREEN,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    export_conversations, _parse_period, print_update_notice,
)

TOOL_COLORS["Gemini CLI"] = GREEN

_BASE = Path.home() / ".gemini" / "tmp"


# ─── Project resolution ───────────────────────────────────────────────────────

def _build_hash_map() -> dict[str, str]:
    """Build {sha256(path): path} for directories under home."""
    result: dict[str, str] = {}
    home = Path.home()
    result[hashlib.sha256(str(home).encode()).hexdigest()] = str(home)
    try:
        for candidate in home.iterdir():
            if candidate.is_dir() and not candidate.name.startswith("."):
                h = hashlib.sha256(str(candidate).encode()).hexdigest()
                result[h] = str(candidate)
                try:
                    for sub in candidate.iterdir():
                        if sub.is_dir():
                            h2 = hashlib.sha256(str(sub).encode()).hexdigest()
                            result[h2] = str(sub)
                except PermissionError:
                    pass
    except PermissionError:
        pass
    return result


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_gemini() -> list[dict]:
    """Scan Gemini CLI session JSON files for token usage."""
    if not _BASE.exists():
        return []

    hash_map = _build_hash_map()
    records = []

    for proj_dir in _BASE.iterdir():
        if not proj_dir.is_dir():
            continue
        project = hash_map.get(proj_dir.name, proj_dir.name)
        chats_dir = proj_dir / "chats"
        if not chats_dir.exists():
            continue

        for session_file in chats_dir.glob("session-*.json"):
            try:
                data = json.loads(open(session_file, errors="replace").read())
                messages = data if isinstance(data, list) else data.get("messages", [])
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    tok = m.get("tokens")
                    if not tok or not isinstance(tok, dict):
                        continue
                    ts_str = m.get("timestamp", "")
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    model = m.get("model", "gemini-unknown")
                    tokens = {
                        "input":       tok.get("input", 0),
                        "output":      tok.get("output", 0),
                        "cache_read":  tok.get("cached", 0),
                        "cache_write": 0,
                    }
                    if tokens["input"] == 0 and tokens["output"] == 0:
                        continue
                    records.append({
                        "tool":    "Gemini CLI",
                        "model":   model,
                        "project": project,
                        "ts":      ts,
                        **tokens,
                        "cost":    compute_cost(tokens, model),
                    })
            except (OSError, json.JSONDecodeError):
                continue

    return records


def scan_speed_gemini() -> list[dict]:
    """Extract output speed from Gemini CLI session files."""
    if not _BASE.exists():
        return []

    results = []
    for proj_dir in _BASE.iterdir():
        if not proj_dir.is_dir():
            continue
        chats_dir = proj_dir / "chats"
        if not chats_dir.exists():
            continue
        for session_file in chats_dir.glob("session-*.json"):
            try:
                data = json.loads(open(session_file, errors="replace").read())
                messages = data if isinstance(data, list) else data.get("messages", [])
                prev_ts = None
                for m in messages:
                    if not isinstance(m, dict):
                        continue
                    tok = m.get("tokens")
                    ts_str = m.get("timestamp", "")
                    if not ts_str:
                        continue
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        continue
                    if not tok or not isinstance(tok, dict):
                        prev_ts = ts
                        continue
                    out = tok.get("output", 0)
                    if out < 10 or prev_ts is None:
                        prev_ts = ts
                        continue
                    dt = (ts - prev_ts).total_seconds()
                    if 0.5 < dt < 300:
                        results.append({
                            "tool":     "Gemini CLI",
                            "model":    m.get("model", "gemini-unknown"),
                            "ts":       ts,
                            "tokens":   out,
                            "duration": dt,
                            "speed":    out / dt,
                            "ttft":     None,
                        })
                    prev_ts = ts
            except (OSError, json.JSONDecodeError):
                continue

    return results


def _extract_exchanges_gemini() -> list[dict]:
    """Extract exchanges from Gemini CLI session files."""
    if not _BASE.exists():
        return []

    hash_map = _build_hash_map()
    exchanges = []

    for proj_dir in _BASE.iterdir():
        if not proj_dir.is_dir():
            continue
        project = hash_map.get(proj_dir.name, proj_dir.name)
        chats_dir = proj_dir / "chats"
        if not chats_dir.exists():
            continue

        for session_file in sorted(chats_dir.glob("session-*.json")):
            try:
                data = json.loads(open(session_file, errors="replace").read())
                messages = data if isinstance(data, list) else data.get("messages", [])
            except (OSError, json.JSONDecodeError):
                continue

            current = None

            for m in messages:
                if not isinstance(m, dict):
                    continue
                msg_type = m.get("type", "")
                content = m.get("content", "").strip()
                ts_str = m.get("timestamp", "")
                ts = None
                if ts_str:
                    try:
                        ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError):
                        pass

                if msg_type == "user":
                    if current:
                        exchanges.append(current)
                    current = {
                        "user_text":       content,
                        "assistant_texts": [],
                        "tool_errors":     [],
                        "tools_used":      defaultdict(int),
                        "num_turns":       0,
                        "model":           "gemini-unknown",
                        "project":         project,
                        "ts":              ts,
                        "tokens":          {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                        "cost":            0.0,
                    }

                elif msg_type == "gemini" and current is not None:
                    current["num_turns"] += 1
                    model = m.get("model", "")
                    if model:
                        current["model"] = model
                    if content:
                        current["assistant_texts"].append(content)

                    # Tool calls
                    for tc in m.get("toolCalls", []):
                        if isinstance(tc, dict):
                            name = tc.get("name", "tool")
                            current["tools_used"][name] += 1

                    # Token data
                    tok = m.get("tokens")
                    if tok and isinstance(tok, dict):
                        inp = tok.get("input", 0)
                        out = tok.get("output", 0)
                        cached = tok.get("cached", 0)
                        if inp > 0 or out > 0:
                            current["tokens"]["input"]      += inp
                            current["tokens"]["output"]     += out
                            current["tokens"]["cache_read"] += cached
                            current["cost"] += compute_cost(
                                {"input": inp, "output": out,
                                 "cache_read": cached, "cache_write": 0},
                                current["model"],
                            )

            if current:
                exchanges.append(current)

    return exchanges


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
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

    _add("Gemini CLI", _extract_exchanges_gemini())
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — Gemini CLI{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.gemini/tmp/...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not _BASE.exists():
        print(f"  {DIM}Gemini CLI not found at {_BASE}{RESET}\n")
        return

    records = scan_gemini()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {GREEN}●{RESET} {'Gemini CLI':<12} {len(records):>6} records from ~/.gemini/")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    speed_records = scan_speed_gemini()
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]

    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label, tool_filter)


# ─── CLI ──────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"gemini": "Gemini CLI", "gemini-cli": "Gemini CLI"}

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
    valid = ", ".join(sorted(set(_TOOL_ALIASES.values())))
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: {valid}")


def show_help():
    print(f"""
{BOLD}gemini-token-usage{RESET} — Aggregate and analyze Gemini CLI token consumption.

{BOLD}MODES{RESET}
  gemini-token-usage                            Aggregated overview (period, project, model, speed)
  gemini-token-usage --prompts  [-p]            Per-exchange detail (text, turns, tokens, tools, cost)
  gemini-token-usage --anomalies                Technical anomaly detection
  gemini-token-usage --plan                     Cost breakdown + optimization tips
  gemini-token-usage --export   [file.json]     Export all exchanges to JSON
  gemini-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}
  --period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days", year

{BOLD}DATA SOURCE{RESET}
  {GREEN}Gemini CLI{RESET}    {DIM}~/.gemini/tmp/{{project_hash}}/chats/session-*.json{RESET}
               ✓ Tokens ✓ Text ✓ Tools ✓ Speed
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
        print(f"  Run {BOLD}gemini-token-usage --help{RESET} for usage.\n")
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
