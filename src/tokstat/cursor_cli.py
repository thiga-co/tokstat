#!/usr/bin/env python3
"""
cursor-token-usage — Analyze Cursor agent session activity from local transcripts.

Data source: ~/.cursor/projects/*/agent-transcripts/**/*.jsonl
Token counts are NOT available locally (Cursor tracks them server-side).
This tool shows exchanges, tool calls, and session activity.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tokstat.cli import __version__
from tokstat._core import (
    BOLD, DIM, RESET, BLUE, YELLOW, RED, GREEN, CYAN,
    TOOL_COLORS,
    resolve_period,
    shorten_path, print_table, fmt_tokens,
    _parse_period, print_update_notice,
)

TOOL_COLORS["Cursor"] = BLUE

_TRANSCRIPTS_BASE = Path.home() / ".cursor" / "projects"


# ─── Project path decoding ────────────────────────────────────────────────────

def _decode_project_path(dirname: str) -> str:
    """Decode Cursor's project directory name to a filesystem path.

    Cursor encodes paths as: separators and spaces → '-', so decoding is
    ambiguous. We try to match against existing directories.
    """
    # Try matching against real paths under home
    home = Path.home()
    # The directory name starts with "Users-<username>-..."
    # Replace all '-' with '/' and prepend '/'
    candidate = "/" + dirname.replace("-", "/")
    if Path(candidate).exists():
        return candidate

    # Try with spaces: some '-' might be spaces
    # Walk down the path matching greedily
    parts = dirname.split("-")
    current = Path("/")
    i = 0
    while i < len(parts):
        # Try joining increasing numbers of parts with spaces
        matched = False
        for j in range(len(parts), i, -1):
            name = " ".join(parts[i:j])
            candidate = current / name
            if candidate.exists():
                current = candidate
                i = j
                matched = True
                break
            name = "-".join(parts[i:j])
            candidate = current / name
            if candidate.exists():
                current = candidate
                i = j
                matched = True
                break
        if not matched:
            # Fallback: use the raw decoded path
            return "/" + dirname.replace("-", "/")

    return str(current)


# ─── Session scanner ──────────────────────────────────────────────────────────

def scan_cursor_sessions(cutoff: datetime, cutoff_end: datetime | None = None) -> list[dict]:
    """Scan Cursor agent-transcript JSONL files and return session exchanges.

    Each exchange = one user turn → all following assistant turns.
    Returns a list of exchange dicts.
    """
    if not _TRANSCRIPTS_BASE.exists():
        return []

    exchanges = []

    for proj_dir in _TRANSCRIPTS_BASE.iterdir():
        if not proj_dir.is_dir():
            continue

        project_path = _decode_project_path(proj_dir.name)

        # Scan agent-tools for tool output sizes (WebFetch results etc.)
        tools_dir = proj_dir / "agent-tools"
        tool_output_chars = sum(
            f.stat().st_size for f in tools_dir.iterdir()
            if f.is_file()
        ) if tools_dir.exists() else 0

        at_dir = proj_dir / "agent-transcripts"
        if not at_dir.exists():
            continue

        for session_dir in at_dir.iterdir():
            if not session_dir.is_dir():
                continue
            jsonl = session_dir / f"{session_dir.name}.jsonl"
            if not jsonl.exists():
                continue

            # Use file modification time as session timestamp
            session_ts = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
            if session_ts < cutoff:
                continue
            if cutoff_end and session_ts >= cutoff_end:
                continue

            lines = []
            for raw in open(jsonl, errors="replace"):
                raw = raw.strip()
                if raw:
                    try:
                        lines.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass

            if not lines:
                continue

            # Parse exchanges: user turn → assistant turn(s)
            current = None
            context_chars = 0

            for rec in lines:
                role = rec.get("role")
                content = rec.get("message", {}).get("content", [])
                if not isinstance(content, list):
                    content = []

                msg_chars = sum(len(str(c)) for c in content)

                if role == "user":
                    if current is not None:
                        exchanges.append(current)

                    user_text = ""
                    for c in content:
                        if isinstance(c, dict) and c.get("type") == "text":
                            t = c.get("text", "")
                            # Strip injected tags
                            t = re.sub(r"<user_info>.*?</user_info>", "", t, flags=re.DOTALL)
                            t = re.sub(r"<agent_transcripts>.*?</agent_transcripts>", "", t, flags=re.DOTALL)
                            t = re.sub(r"<user_query>\s*", "", t)
                            t = re.sub(r"\s*</user_query>", "", t)
                            t = t.strip()
                            if t:
                                user_text = t
                                break

                    current = {
                        "tool":          "Cursor",
                        "project":       project_path,
                        "ts":            session_ts,
                        "user_text":     user_text,
                        "assistant_texts": [],
                        "tool_errors":   [],
                        "tools_used":    defaultdict(int),
                        "num_turns":     0,
                        # Token estimates — output only (reliable), input = unknown
                        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                        "cost":          0.0,
                        "_context_chars": context_chars,
                        "_tool_output_chars": tool_output_chars,
                    }
                    context_chars += msg_chars

                elif role == "assistant" and current is not None:
                    current["num_turns"] += 1
                    for c in content:
                        if isinstance(c, dict):
                            if c.get("type") == "text":
                                t = c.get("text", "").strip()
                                if t:
                                    current["assistant_texts"].append(t)
                                    current["tokens"]["output"] += len(t) // 4
                            elif c.get("type") == "tool_use":
                                name = c.get("name", "unknown")
                                current["tools_used"][name] += 1
                    context_chars += msg_chars

            if current is not None:
                exchanges.append(current)

    # Clean up internal keys
    for ex in exchanges:
        ex.pop("_context_chars", None)
        ex.pop("_tool_output_chars", None)

    return exchanges


# ─── Display modes ────────────────────────────────────────────────────────────

def show_overview(period_name: str | None = None):
    print(f"\n{BOLD} Cursor Session Activity{RESET}")
    print(f"{DIM}  Token counts are not available locally — showing exchange & tool activity.{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not _TRANSCRIPTS_BASE.exists():
        print(f"  {DIM}Cursor not found at {_TRANSCRIPTS_BASE}{RESET}\n")
        return

    exchanges = scan_cursor_sessions(cutoff, cutoff_end)

    if not exchanges:
        print(f"  {YELLOW}No Cursor sessions found for {period_label}.{RESET}\n")
        return

    print(f"  {BLUE}●{RESET} Cursor  {len(exchanges):>4} exchanges   Period: {BOLD}{period_label}{RESET}\n")

    # Group by project
    by_project: dict[str, list[dict]] = {}
    for ex in exchanges:
        by_project.setdefault(ex["project"], []).append(ex)

    headers = ["Project", "Sessions", "Exchanges", "Turns", "Top tools"]
    aligns  = ["<",       ">",        ">",         ">",     "<"]
    rows = []

    for proj, exs in sorted(by_project.items(), key=lambda x: -len(x[1])):
        total_turns = sum(e["num_turns"] for e in exs)
        all_tools: dict[str, int] = defaultdict(int)
        for ex in exs:
            for t, c in ex["tools_used"].items():
                all_tools[t] += c
        top = sorted(all_tools, key=lambda t: -all_tools[t])[:3]
        tools_str = ", ".join(f"{t}({all_tools[t]})" for t in top) or DIM + "—" + RESET
        dates = sorted(set(e["ts"].strftime("%Y-%m-%d") for e in exs))
        rows.append([
            shorten_path(proj, 42),
            str(len(dates)),
            str(len(exs)),
            str(total_turns),
            tools_str,
        ])

    print_table(headers, rows, aligns)

    # Total tool usage
    all_tools: dict[str, int] = defaultdict(int)
    for ex in exchanges:
        for t, c in ex["tools_used"].items():
            all_tools[t] += c

    if all_tools:
        print(f"\n  {BOLD}Tool usage{RESET}")
        tool_rows = [[t, str(c)] for t, c in
                     sorted(all_tools.items(), key=lambda x: -x[1])[:10]]
        print_table(["Tool", "Calls"], tool_rows, ["<", ">"])

    total_turns = sum(e["num_turns"] for e in exchanges)
    total_tools = sum(sum(e["tools_used"].values()) for e in exchanges)
    first_ts = min(e["ts"] for e in exchanges)
    last_ts  = max(e["ts"] for e in exchanges)
    print(f"\n  {BOLD}Total:{RESET} {len(exchanges)} exchanges  {total_turns} turns  {total_tools} tool calls")
    print(f"  {DIM}Period: {first_ts.strftime('%Y-%m-%d')} to {last_ts.strftime('%Y-%m-%d')}{RESET}\n")


def show_prompts(period_name: str | None = None):
    print(f"\n{BOLD} Cursor Exchanges{RESET}")
    print(f"{DIM}  Token counts not available locally.{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    exchanges = scan_cursor_sessions(cutoff, cutoff_end)
    if not exchanges:
        print(f"  {YELLOW}No exchanges found.{RESET}\n")
        return

    print(f"  Period: {BOLD}{period_label}{RESET}\n")

    by_project: dict[str, list[dict]] = {}
    for ex in exchanges:
        by_project.setdefault(ex["project"], []).append(ex)

    for proj, exs in sorted(by_project.items(), key=lambda x: -len(x[1])):
        proj_display = shorten_path(proj, 50)
        total_turns = sum(e["num_turns"] for e in exs)
        print(f"  {BLUE}{BOLD}Cursor{RESET} {DIM}{proj_display}{RESET}  {CYAN}{len(exs)} exchanges{RESET}  {total_turns} turns")

        headers = ["#", "Date", "Input text", "Turns", "Tool calls", "Top tools"]
        aligns  = [">", "<",    "<",          ">",     ">",          "<"]
        rows = []

        for i, ex in enumerate(sorted(exs, key=lambda e: e["ts"]), 1):
            user_text = ex.get("user_text", "").replace("\n", " ")
            if len(user_text) > 50:
                user_text = user_text[:47] + "..."
            if not user_text:
                user_text = DIM + "(no text)" + RESET

            ts_str = ex["ts"].strftime("%m-%d %H:%M")
            tools = ex.get("tools_used", {})
            total_tool_calls = sum(tools.values())
            top = sorted(tools, key=lambda t: -tools[t])[:3]
            tools_str = " ".join(t for t in top) if top else DIM + "—" + RESET

            rows.append([str(i), ts_str, user_text,
                         str(ex.get("num_turns", 0)),
                         str(total_tool_calls), tools_str])

        print_table(headers, rows, aligns)
        print()


# ─── CLI ─────────────────────────────────────────────────────────────────────

_KNOWN_FLAGS = {"--help", "-h", "--prompts", "-p", "--period", "--since"}


def show_help():
    print(f"""
{BOLD}cursor-token-usage{RESET} — Analyze Cursor agent session activity.

{BOLD}NOTE{RESET}  {DIM}Token counts are tracked server-side by Cursor and are not available
      locally. This tool shows session activity, exchanges, and tool usage.
      For exact token counts, use the Cursor dashboard (cursor.com/settings/usage).{RESET}

{BOLD}MODES{RESET}
  cursor-token-usage              Session activity overview (projects, exchanges, tools)
  cursor-token-usage --prompts    Per-exchange detail with tool calls
  cursor-token-usage --help       This help

{BOLD}FILTERS{RESET}
  --period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days", year

{BOLD}DATA SOURCE{RESET}
  {BLUE}Cursor{RESET}  {DIM}~/.cursor/projects/*/agent-transcripts/{RESET}
""")


def cli():
    args = sys.argv[1:]
    if "--help" in args or "-h" in args:
        show_help()
        return

    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"\n  {RED}Unknown option(s): {', '.join(unknown)}{RESET}")
        print(f"  Run {BOLD}cursor-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)

    if "--prompts" in args or "-p" in args:
        show_prompts(period)
    else:
        show_overview(period)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
