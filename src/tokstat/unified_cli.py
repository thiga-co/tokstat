#!/usr/bin/env python3
"""
tokstat — Unified view of token consumption across all supported AI coding
assistants (Claude Code, Codex, Cursor, Kiro, Gemini CLI).

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import io
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime
from pathlib import Path

from tokstat.cli import (
    __version__,
    scan_claude_code, scan_speed_claude_code,
    _collect_all_exchanges as _collect_claude,
)
from tokstat.codex_cli import (
    scan_codex, scan_speed_codex,
    _collect_all_exchanges as _collect_codex,
)
from tokstat.cursor_cli import (
    scan_cursor,
    _collect_all_exchanges as _collect_cursor,
)
from tokstat.kiro_cli import (
    scan_kiro,
    _collect_all_exchanges as _collect_kiro,
)
from tokstat.gemini_cli import (
    scan_gemini, scan_speed_gemini,
    _collect_all_exchanges as _collect_gemini,
)
from tokstat.opencode_cli import (
    scan_opencode, scan_speed_opencode,
    _collect_all_exchanges as _collect_opencode,
)
from tokstat.claude_web_cli import (
    scan_claude_web,
    _collect_all_exchanges as _collect_claude_web,
)
from tokstat.chatgpt_web_cli import (
    scan_chatgpt_web,
    _collect_all_exchanges as _collect_chatgpt_web,
)

from tokstat._core import (
    BOLD, DIM, RESET, YELLOW, RED,
    TOOL_COLORS, PRICING,
    load_pricing,
    resolve_period,
    _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    export_conversations, _parse_period, print_update_notice,
    compute_overview_state,
)


# Map each known tool name → (scanner, speed_scanner_or_None, collector, data_label)
_TOOLS = [
    ("Claude Code", scan_claude_code, scan_speed_claude_code, _collect_claude, "~/.claude/"),
    ("Codex",       scan_codex,       scan_speed_codex,       _collect_codex,  "~/.codex/"),
    ("Cursor",      scan_cursor,      None,                   _collect_cursor, "~/.cursor/"),
    ("Kiro",        scan_kiro,        None,                   _collect_kiro,
     "~/Library/Application Support/Kiro/"),
    ("Gemini CLI",  scan_gemini,      scan_speed_gemini,      _collect_gemini, "~/.gemini/"),
    ("opencode",    scan_opencode,    scan_speed_opencode,    _collect_opencode,
     "~/.local/share/opencode/"),
    ("Claude.ai",   scan_claude_web,  None,                   _collect_claude_web,
     "claude.ai (web)"),
    ("ChatGPT",     scan_chatgpt_web, None,                   _collect_chatgpt_web,
     "chatgpt.com (web)"),
]

_TOOL_ALIASES = {
    "claude": "Claude Code", "claude-code": "Claude Code", "claudecode": "Claude Code",
    "codex":  "Codex",       "openai":      "Codex",
    "cursor": "Cursor",
    "kiro":   "Kiro",
    "gemini": "Gemini CLI",  "gemini-cli":  "Gemini CLI",
    "opencode": "opencode",  "open-code":   "opencode",
    "claude.ai": "Claude.ai", "claude-web": "Claude.ai", "claudeai": "Claude.ai",
    "chatgpt":   "ChatGPT",   "chatgpt.com": "ChatGPT",  "chatgpt-web": "ChatGPT",
}


def _scan_all(tool_filter: str | None) -> tuple[list[dict], list[dict], list[tuple[str, int, str]]]:
    """Run every registered scanner. Returns (records, speed_records, per_tool_counts)."""
    records: list[dict] = []
    speed_records: list[dict] = []
    counts: list[tuple[str, int, str]] = []  # (tool, n_records, data_path)

    for tool_name, scan_fn, speed_fn, _collect, data_path in _TOOLS:
        if tool_filter and tool_name != tool_filter:
            continue
        try:
            tool_records = scan_fn()
        except Exception:
            tool_records = []
        records.extend(tool_records)
        counts.append((tool_name, len(tool_records), data_path))
        if speed_fn is not None:
            try:
                speed_records.extend(speed_fn())
            except Exception:
                pass

    return records, speed_records, counts


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Aggregate exchanges from every registered tool."""
    all_exchanges: list[dict] = []
    tool_counts: dict[str, int] = {}

    for tool_name, _scan, _speed, collect_fn, _path in _TOOLS:
        if tool_filter and tool_name != tool_filter:
            continue
        try:
            exchanges, counts = collect_fn(cutoff, tool_filter, cutoff_end)
        except Exception:
            continue
        all_exchanges.extend(exchanges)
        for k, v in counts.items():
            tool_counts[k] = tool_counts.get(k, 0) + v

    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def _render_overview(period_name: str | None, tool_filter: str | None,
                     header_suffix: str = "",
                     prev_state: dict | None = None) -> tuple[bool, dict | None]:
    """Scan all sources and print the overview tables.

    Returns (ok, current_state). `current_state` is the snapshot of the
    aggregated metrics — pass it back as `prev_state` next call to highlight
    rows that changed.
    """
    print(f"\n{BOLD} Token Usage — All tools{RESET}{header_suffix}")
    print(f"{DIM}  Scanning all data sources...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return False, None

    records, speed_records, counts = _scan_all(tool_filter)

    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]
    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)

    for tool_name, n_total, data_path in counts:
        if n_total == 0:
            continue
        n_in_period = sum(1 for r in records if r.get("tool") == tool_name)
        color = TOOL_COLORS.get(tool_name, "")
        print(f"  {color}●{RESET} {tool_name:<12} {n_in_period:>6} records from {data_path}")

    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    state = compute_overview_state(records, exchanges, cutoff, cutoff_end, period_label)
    changed_keys: set | None = None
    if prev_state is not None:
        changed_keys = {k for k, v in state.items() if prev_state.get(k) != v}

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return True, state

    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges, changed_keys=changed_keys)
    return True, state


def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    _render_overview(period_name, tool_filter)


def watch(period_name: str | None, tool_filter: str | None, interval: float):
    """Refresh the overview every `interval` seconds until Ctrl+C.

    Uses cursor-home + erase-to-end-of-screen instead of full clear so the
    redraw overwrites in place without flashing. Rows whose aggregated
    metrics changed since the previous tick are marked with a yellow ◆.
    """
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    sys.stdout.write("\033[?25l")  # hide cursor during loop
    sys.stdout.flush()

    iteration = 0
    prev_state: dict | None = None
    try:
        while True:
            iteration += 1
            suffix = (f"  {DIM}— watching, refresh #{iteration} every {interval:g}s "
                      f"(Ctrl+C to stop){RESET}")
            # Render into a buffer so we can rewrite each line with a
            # trailing erase-to-EOL — avoids leftover chars when a new line
            # is shorter than the previous frame's line at that position.
            buf = io.StringIO()
            with redirect_stdout(buf):
                ok, state = _render_overview(period_name, tool_filter,
                                             header_suffix=suffix,
                                             prev_state=prev_state)
            output = buf.getvalue()

            sys.stdout.write("\033[H")  # cursor home, no clear
            for line in output.split("\n"):
                sys.stdout.write(line + "\033[K\n")  # erase rest of line
            sys.stdout.write("\033[J")  # erase any leftover lines below
            sys.stdout.flush()
            if not ok:
                return
            prev_state = state
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n  {DIM}Stopped after {iteration} refresh(es).{RESET}\n")
    finally:
        sys.stdout.write("\033[?25h")  # show cursor again
        sys.stdout.flush()


# ─── CLI ─────────────────────────────────────────────────────────────────────

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--export", "--period", "--since", "--tool", "--watch", "-w",
}

_DEFAULT_WATCH_INTERVAL = 5.0


def _parse_watch_interval(args: list[str]) -> float | None:
    """Return refresh interval in seconds if --watch / -w is set, else None."""
    flag = None
    for f in ("--watch", "-w"):
        if f in args:
            flag = f
            break
    if flag is None:
        return None
    idx = args.index(flag)
    if idx + 1 < len(args):
        nxt = args[idx + 1]
        if not nxt.startswith("-"):
            try:
                val = float(nxt)
                if val < 1:
                    raise ValueError
                return val
            except ValueError:
                pass
    return _DEFAULT_WATCH_INTERVAL


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
    valid = ", ".join(sorted({n for n in _TOOL_ALIASES.values()}))
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: {valid}")


def show_help():
    print(f"""
{BOLD}tokstat{RESET} — Unified view across all supported AI coding assistants.

{BOLD}MODES{RESET}
  tokstat                                  Aggregated overview (period, project, model)
  tokstat --prompts  [-p]                  Per-exchange detail across all tools
  tokstat --anomalies                      Technical anomaly detection
  tokstat --plan                           Cost breakdown + optimization tips
  tokstat --export   [file.json]           Export all exchanges to JSON
  tokstat --watch    [-w] [SECONDS]        Refresh overview live (default 5s, Ctrl+C to stop)
  tokstat --version  [-V]                  Show version
  tokstat --help     [-h]                  This help

{BOLD}FILTERS{RESET}
  --period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days", year
  --tool   <name>      claude, codex, cursor, kiro, gemini, opencode,
                       claude.ai, chatgpt (default: all)

{BOLD}TOOLS COVERED{RESET}
  Claude Code  ~/.claude/projects/
  Codex        ~/.codex/sessions/
  Cursor       ~/.cursor/projects/                                    (estimates)
  Kiro         ~/Library/Application Support/Kiro/...                 (estimates)
  Gemini CLI   ~/.gemini/tmp/
  opencode     ~/.local/share/opencode/storage/
  Claude.ai    private https://claude.ai/api/ (session cookie, estimates)
  ChatGPT      private https://chatgpt.com/backend-api/ (session cookie, estimates)

{BOLD}SEE ALSO{RESET}
  claude-token-usage, codex-token-usage, cursor-token-usage,
  kiro-token-usage, gemini-token-usage, opencode-token-usage,
  claude-web-token-usage, chatgpt-web-token-usage — single-tool variants.
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
        print(f"  Run {BOLD}tokstat --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)
    try:
        tool = _parse_tool(args)
    except ValueError as e:
        print(f"\n  {RED}{e}{RESET}\n")
        sys.exit(1)

    watch_interval = _parse_watch_interval(args)
    if watch_interval is not None:
        if any(f in args for f in ("--prompts", "-p", "--anomalies", "--plan", "--export")):
            print(f"\n  {RED}--watch only applies to the default overview mode.{RESET}\n")
            sys.exit(1)
        watch(period, tool, watch_interval)
        return

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
