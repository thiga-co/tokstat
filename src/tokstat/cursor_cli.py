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
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    shorten_path, print_table, fmt_tokens, fmt_cost,
    _parse_period, print_update_notice,
)

# ─── Token estimation heuristics ─────────────────────────────────────────────
# Cursor's agent system prompt is large (~3k tokens).
# Tool outputs are NOT stored locally except WebFetch (in agent-tools/).
# We use per-tool averages based on empirical observation.
_CURSOR_SYSTEM_PROMPT_TOKENS = 3_000
_TOOL_OUTPUT_TOKENS: dict[str, int] = {
    "Shell":           3_000,   # shell output (commands, file listings, etc.)
    "ReadFile":        5_000,   # file contents (varies widely)
    "WebFetch":        7_000,   # web page content (use agent-tools size when available)
    "Glob":              300,
    "ApplyPatch":        500,
    "ReadLints":         300,
    "GenerateImage":     200,
    "Search":          1_000,
    "default":           800,
}

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


# ─── Token estimation ────────────────────────────────────────────────────────

def _finalize_estimates(exchanges: list[dict]) -> None:
    """Compute estimated input tokens and cost for each exchange.

    Strategy:
      input ≈ system_prompt + accumulated_context_text + tool_outputs_heuristic
      output ≈ response_text / 4  (already set during parsing)

    All values are rough estimates — real counts are 5-15x higher in practice
    because tool outputs (Shell, ReadFile) are not stored locally.
    """
    webfetch_used: dict[str, bool] = {}  # track per-project WebFetch allocation

    for ex in exchanges:
        ctx_tokens = ex.pop("_context_chars", 0) // 4
        webfetch_tokens = ex.pop("_webfetch_tokens", 0)
        model = ex.pop("_model", "gpt-4o")

        # System prompt constant
        input_tokens = _CURSOR_SYSTEM_PROMPT_TOKENS + ctx_tokens

        # Tool output heuristics
        webfetch_count = 0
        for tool_name, count in ex["tools_used"].items():
            if tool_name == "WebFetch":
                webfetch_count += count
            else:
                est = _TOOL_OUTPUT_TOKENS.get(tool_name, _TOOL_OUTPUT_TOKENS["default"])
                input_tokens += est * count

        # Use actual agent-tools size for WebFetch when available, else heuristic
        proj = ex["project"]
        if webfetch_count > 0:
            if webfetch_tokens > 0 and not webfetch_used.get(proj):
                webfetch_used[proj] = True
                input_tokens += webfetch_tokens
            else:
                input_tokens += _TOOL_OUTPUT_TOKENS["WebFetch"] * webfetch_count

        ex["tokens"]["input"] = input_tokens
        ex["cost"] = compute_cost(ex["tokens"], model)


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

        # Collect agent-tools file sizes (WebFetch results stored locally)
        tools_dir = proj_dir / "agent-tools"
        webfetch_tokens_available = 0
        if tools_dir.exists():
            for f in tools_dir.iterdir():
                if f.is_file():
                    webfetch_tokens_available += f.stat().st_size // 4

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
                        "tool":            "Cursor",
                        "project":         project_path,
                        "ts":              session_ts,
                        "user_text":       user_text,
                        "assistant_texts": [],
                        "tool_errors":     [],
                        "tools_used":      defaultdict(int),
                        "num_turns":       0,
                        "tokens":          {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                        "cost":            0.0,
                        "_context_chars":  context_chars,
                        "_webfetch_tokens": webfetch_tokens_available,
                        "_model":          "gpt-4o",  # Cursor default; refined below
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

    # Compute input token estimates and costs
    _finalize_estimates(exchanges)

    return exchanges


# ─── Display modes ────────────────────────────────────────────────────────────

def show_overview(period_name: str | None = None):
    print(f"\n{BOLD} Cursor Session Activity{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    print(f"{DIM}  Token estimates: context + tool heuristics (5-15x underestimate possible){RESET}\n")

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

    headers = ["Project", "Exchanges", "Input [est]", "Output [est]", "Cost [est]", "Top tools"]
    aligns  = ["<",       ">",         ">",           ">",            ">",           "<"]
    rows = []

    for proj, exs in sorted(by_project.items(), key=lambda x: -sum(e["cost"] for e in x[1])):
        all_tools: dict[str, int] = defaultdict(int)
        for ex in exs:
            for t, c in ex["tools_used"].items():
                all_tools[t] += c
        top = sorted(all_tools, key=lambda t: -all_tools[t])[:3]
        tools_str = ", ".join(f"{t}({all_tools[t]})" for t in top) or DIM + "—" + RESET
        total_in   = sum(e["tokens"]["input"] for e in exs)
        total_out  = sum(e["tokens"]["output"] for e in exs)
        total_cost = sum(e["cost"] for e in exs)
        rows.append([
            shorten_path(proj, 38),
            str(len(exs)),
            fmt_tokens(total_in),
            fmt_tokens(total_out),
            fmt_cost(total_cost),
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
    total_in    = sum(e["tokens"]["input"] for e in exchanges)
    total_out   = sum(e["tokens"]["output"] for e in exchanges)
    total_cost  = sum(e["cost"] for e in exchanges)
    first_ts = min(e["ts"] for e in exchanges)
    last_ts  = max(e["ts"] for e in exchanges)
    print(f"\n  {BOLD}Total [est]:{RESET} {fmt_tokens(total_in + total_out)} tokens  {fmt_cost(total_cost)}  {total_turns} turns")
    print(f"  {DIM}Period: {first_ts.strftime('%Y-%m-%d')} to {last_ts.strftime('%Y-%m-%d')}{RESET}")
    print(f"  {DIM}⚠ Estimates only — tool outputs (Shell/ReadFile) not stored locally.{RESET}\n")


def show_prompts(period_name: str | None = None):
    print(f"\n{BOLD} Cursor Exchanges{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    print(f"{DIM}  Token estimates only — real counts tracked server-side by Cursor.{RESET}\n")

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

    for proj, exs in sorted(by_project.items(), key=lambda x: -sum(e["cost"] for e in x[1])):
        proj_display = shorten_path(proj, 50)
        total_turns = sum(e["num_turns"] for e in exs)
        total_cost  = sum(e["cost"] for e in exs)
        print(f"  {BLUE}{BOLD}Cursor{RESET} {DIM}{proj_display}{RESET}  "
              f"{CYAN}{len(exs)} exchanges{RESET}  {total_turns} turns  "
              f"{BOLD}{fmt_cost(total_cost)}{RESET} {DIM}[est]{RESET}")

        headers = ["#", "Date", "Input text", "Turns", "Input[est]", "Output[est]", "Cost[est]", "Tools"]
        aligns  = [">", "<",    "<",          ">",     ">",          ">",           ">",          "<"]
        rows = []

        for i, ex in enumerate(sorted(exs, key=lambda e: e["ts"]), 1):
            user_text = ex.get("user_text", "").replace("\n", " ")
            if len(user_text) > 45:
                user_text = user_text[:42] + "..."
            if not user_text:
                user_text = DIM + "(no text)" + RESET

            ts_str = ex["ts"].strftime("%m-%d %H:%M")
            tools = ex.get("tools_used", {})
            top = sorted(tools, key=lambda t: -tools[t])[:3]
            tools_str = " ".join(t for t in top) if top else DIM + "—" + RESET
            tok = ex.get("tokens", {})

            rows.append([str(i), ts_str, user_text,
                         str(ex.get("num_turns", 0)),
                         fmt_tokens(tok.get("input", 0)),
                         fmt_tokens(tok.get("output", 0)),
                         fmt_cost(ex.get("cost", 0)),
                         tools_str])

        print_table(headers, rows, aligns)
        print()


# ─── CLI ─────────────────────────────────────────────────────────────────────

_KNOWN_FLAGS = {"--help", "-h", "--prompts", "-p", "--period", "--since"}


def show_help():
    print(f"""
{BOLD}cursor-token-usage{RESET} — Analyze Cursor agent session activity.

{BOLD}NOTE{RESET}  {DIM}Cursor tracks token counts server-side. This tool shows estimated tokens
      based on conversation text + tool output heuristics. Estimates can be
      5-15x lower than reality (Shell/ReadFile outputs not stored locally).
      For exact counts: cursor.com/settings/usage → Export CSV.{RESET}

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
