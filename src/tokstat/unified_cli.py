#!/usr/bin/env python3
"""
tokstat — Unified view of token consumption across all supported AI coding
assistants (Claude Code, Codex, Cursor, Kiro, Gemini CLI, Antigravity).

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import inspect
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
    _DB as _OPENCODE_DB, _MSG_BASE as _OPENCODE_MSG_BASE,
)
from tokstat.claude_web_cli import (
    scan_claude_web,
    _collect_all_exchanges as _collect_claude_web,
)
from tokstat.chatgpt_web_cli import (
    scan_chatgpt_web,
    _collect_all_exchanges as _collect_chatgpt_web,
)
from tokstat.antigravity_cli import (
    scan_antigravity, scan_speed_antigravity,
    _collect_all_exchanges as _collect_antigravity,
    _CONV_DIR as _AG_CONV_DIR,
)

from tokstat._core import (
    BOLD, DIM, RESET, YELLOW, RED,
    TOOL_COLORS, PRICING,
    load_pricing,
    resolve_period,
    _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact, show_tool_use,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    print_retention_alerts,
    compute_overview_state,
    tstamp, timing_enabled, tstamp_scan,
)


# Map each known tool name → (scanner, speed_scanner_or_None, collector, data_label, presence_probe)
# `presence_probe` cheaply reports whether the tool has any data on disk, so a
# tool with nothing in the selected period still appears (as "0 records").
_TOOLS = [
    ("Claude Code",  scan_claude_code,   scan_speed_claude_code, _collect_claude,      "~/.claude/", None),
    ("Codex",        scan_codex,         scan_speed_codex,       _collect_codex,       "~/.codex/", None),
    ("Cursor",       scan_cursor,        None,                   _collect_cursor,
     "~/Library/.../Cursor/", None),
    ("Kiro",         scan_kiro,          None,                   _collect_kiro,
     "~/Library/Application Support/Kiro/", None),
    ("Gemini CLI",   scan_gemini,        scan_speed_gemini,      _collect_gemini,      "~/.gemini/", None),
    ("Antigravity",  scan_antigravity,   scan_speed_antigravity, _collect_antigravity, "~/.gemini/antigravity-cli/",
     lambda: _path_exists(_AG_CONV_DIR)),
    ("opencode",     scan_opencode,      scan_speed_opencode,    _collect_opencode,
     "~/.local/share/opencode/",
     lambda: _path_exists(_OPENCODE_DB) or _path_exists(_OPENCODE_MSG_BASE)),
    ("Claude.ai",    scan_claude_web,    None,                   _collect_claude_web,
     "claude.ai (web)", None),
    ("ChatGPT",      scan_chatgpt_web,   None,                   _collect_chatgpt_web,
     "chatgpt.com (web)", None),
]


def _path_exists(path) -> bool:
    try:
        return path.exists()
    except OSError:
        return False


def _supports_cutoff(fn) -> bool:
    """True when a scanner accepts cutoff=... (period-aware scans skip whole
    sources that cannot hold in-period data)."""
    if fn is None:
        return False
    try:
        return "cutoff" in inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False


_TOOL_ALIASES = {
    "claude": "Claude Code", "claude-code": "Claude Code", "claudecode": "Claude Code",
    "codex":  "Codex",       "openai":      "Codex",
    "cursor": "Cursor",
    "kiro":   "Kiro",
    "gemini": "Gemini CLI",  "gemini-cli":  "Gemini CLI",
    "antigravity": "Antigravity", "agy": "Antigravity",
    "opencode": "opencode",  "open-code":   "opencode",
    "claude.ai": "Claude.ai", "claude-web": "Claude.ai", "claudeai": "Claude.ai",
    "chatgpt":   "ChatGPT",   "chatgpt.com": "ChatGPT",  "chatgpt-web": "ChatGPT",
}


def _scan_all(tool_filter: str | None, cutoff: datetime | None = None,
              cutoff_end: datetime | None = None) -> tuple[list[dict], list[dict], list[tuple[str, int, str]]]:
    """Run every registered scanner. Returns (records, speed_records, per_tool_counts)."""
    records: list[dict] = []
    speed_records: list[dict] = []
    counts: list[tuple[str, int, str]] = []  # (tool, n_records, data_path)

    for tool_name, scan_fn, speed_fn, _collect, data_path, presence in _TOOLS:
        if tool_filter and tool_name != tool_filter:
            continue
        t_scan = time.monotonic()
        try:
            if _supports_cutoff(scan_fn):
                tool_records = scan_fn(cutoff=cutoff, cutoff_end=cutoff_end)
            else:
                tool_records = scan_fn()
        except Exception:
            tool_records = []
        records.extend(tool_records)
        n_total = len(tool_records)
        if n_total == 0 and presence is not None and presence():
            n_total = 1     # has data on disk, just none in the selected period
        counts.append((tool_name, n_total, data_path))
        tstamp_scan(tool_name, t_scan, len(tool_records))

        if speed_fn is not None:
            t_speed = time.monotonic()
            try:
                if _supports_cutoff(speed_fn):
                    speed = speed_fn(cutoff=cutoff, cutoff_end=cutoff_end)
                else:
                    speed = speed_fn()
                speed_records.extend(speed)
            except Exception:
                speed = []
            tstamp_scan(f"{tool_name} (speed)", t_speed, len(speed))

    return records, speed_records, counts


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Aggregate exchanges from every registered tool."""
    all_exchanges: list[dict] = []
    tool_counts: dict[str, int] = {}

    for tool_name, _scan, _speed, collect_fn, _path, _presence in _TOOLS:
        if tool_filter and tool_name != tool_filter:
            continue
        t_collect = time.monotonic()
        try:
            exchanges, counts = collect_fn(cutoff, tool_filter, cutoff_end)
        except Exception:
            exchanges, counts = [], {}
        tstamp_scan(f"{tool_name} (exchanges)", t_collect, len(exchanges))
        all_exchanges.extend(exchanges)
        for k, v in counts.items():
            tool_counts[k] = tool_counts.get(k, 0) + v

    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


def _span_label(timestamps: list) -> str:
    """Human span between the earliest and latest timestamp: '—' for none,
    days up to ~2 months, then months."""
    if not timestamps:
        return "—"
    d = (max(timestamps) - min(timestamps)).total_seconds() / 86400.0
    if d < 1:
        return "1 day"
    if d < 60:
        return f"{round(d)} days"
    return f"{round(d / 30.44)} months"


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def _render_overview(period_name: str | None, tool_filter: str | None,
                     header_suffix: str = "",
                     prev_state: dict | None = None,
                     by_session: bool = False) -> tuple[bool, dict | None]:
    """Scan all sources and print the overview tables.

    Returns (ok, current_state). `current_state` is the snapshot of the
    aggregated metrics — pass it back as `prev_state` next call to highlight
    rows that changed.
    """
    print(f"\n{tstamp()}{BOLD} Token Usage — All tools{RESET}{header_suffix}")
    print(f"{tstamp()}{DIM}  Scanning all data sources...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return False, None

    records, speed_records, counts = _scan_all(tool_filter, cutoff, cutoff_end)

    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]
    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)

    for tool_name, n_total, data_path in counts:
        if n_total == 0:
            continue
        tool_ts = [r["ts"] for r in records if r.get("tool") == tool_name]
        n_in_period = len(tool_ts)
        color = TOOL_COLORS.get(tool_name, "")
        span = f"{DIM}{_span_label(tool_ts):>10}{RESET}"
        since = (f"{DIM}since {min(tool_ts).strftime('%Y-%m-%d')}{RESET}"
                 if tool_ts else f"{DIM}{'—':>16}{RESET}")
        print(f"{tstamp()}  {color}●{RESET} {tool_name:<12} {n_in_period:>6} records · "
              f"{span} · {since} from {data_path}")

    print()
    print_retention_alerts(name for name, n_total, _ in counts if n_total > 0)

    print(f"  Period: {BOLD}{period_label}{RESET}")

    state = compute_overview_state(records, exchanges, cutoff, cutoff_end, period_label)
    changed_keys: set | None = None
    if prev_state is not None:
        changed_keys = {k for k, v in state.items() if prev_state.get(k) != v}

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return True, state

    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges, changed_keys=changed_keys,
                         by_session=by_session)
    return True, state


def main(period_name: str | None = None, tool_filter: str | None = None,
         by_session: bool = False):
    print(f"{tstamp()}{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"{tstamp()}  {DIM}{len(PRICING)} models loaded{RESET}")
    _render_overview(period_name, tool_filter, by_session=by_session)


def watch(period_name: str | None, tool_filter: str | None, interval: float,
          by_session: bool = False):
    """Refresh the overview every `interval` seconds until Ctrl+C.

    Uses cursor-home + erase-to-end-of-screen instead of full clear so the
    redraw overwrites in place without flashing. Rows whose aggregated
    metrics changed since the previous tick are marked with a yellow ◆.
    """
    print(f"{tstamp()}{DIM}  Loading pricing from LiteLLM...{RESET}")
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
                                             prev_state=prev_state,
                                             by_session=by_session)
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
    "--plan", "--activity", "--total", "--impact", "--by-session", "--tool-use", "--session", "--export", "--period", "--since", "--tool", "--watch", "-w",
}

_DEFAULT_WATCH_INTERVAL = 5.0


def _arg_value(args, flag, default=None):
    """Value following `flag` on the command line, or default."""
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return default


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
  tokstat --by-session                     Overview + a per-session table (all sessions)
  tokstat --prompts  [-p]                  Per-exchange detail across all tools
  tokstat --anomalies                      Technical anomaly detection
  tokstat --activity                       Activity calendar (GitHub-style, by day)
  tokstat --total                          Compact totals (tokens + cost + data span)
  tokstat --tool-use                       Timeline of tool calls (file/command + time)
  tokstat --impact [region]                Energy & CO₂ estimate (EcoLogits;
                                           region: world/eu/france/us/green)
  tokstat --plan                           Cost breakdown + optimization tips
  tokstat --export   [file.json]           Export all exchanges to JSON
  tokstat --watch    [-w] [SECONDS]        Refresh overview live (default 5s, Ctrl+C to stop)
  tokstat --version  [-V]                  Show version
  tokstat --help     [-h]                  This help

{BOLD}FILTERS{RESET}
  --period <period>    all, hour, "5 hours", today, yesterday, "7 days",
                       "30 days", "1 month", "2 months", "3 months",
                       "6 months", year   (partial match works; default: today)
  --tool   <name>      claude, codex, cursor, kiro, gemini, antigravity,
                       opencode, claude.ai, chatgpt (default: all)
  --session <id>       scope --prompts / --tool-use to one session (full id
                       or the short 8-char handle, e.g. 019f6a75)

{BOLD}TOOLS COVERED{RESET}
  Claude Code  ~/.claude/projects/                          exact tokens
  Codex        ~/.codex/sessions/                           exact tokens
  Cursor       Cursor globalStorage/state.vscdb             exact / no data
  Kiro         Kiro .../workspace-sessions/                 activity only
  Gemini CLI   ~/.gemini/tmp/                               exact tokens
  Antigravity  ~/.gemini/antigravity-cli/                   exact tokens
  opencode     ~/.local/share/opencode/ (opencode.db)       exact tokens
  Claude.ai    --import of official export (claude-web-token-usage)
  ChatGPT      --import of official export (chatgpt-web-token-usage)

{BOLD}SEE ALSO{RESET}
  claude-token-usage, codex-token-usage, cursor-token-usage,
  kiro-token-usage, gemini-token-usage, antigravity-token-usage,
  opencode-token-usage, claude-web-token-usage,
  chatgpt-web-token-usage — single-tool variants.
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

    by_session = "--by-session" in args

    watch_interval = _parse_watch_interval(args)
    if watch_interval is not None:
        if any(f in args for f in ("--prompts", "-p", "--anomalies", "--plan", "--export")):
            print(f"\n  {RED}--watch only applies to the default overview mode.{RESET}\n")
            sys.exit(1)
        watch(period, tool, watch_interval, by_session=by_session)
        return

    if "--prompts" in args or "-p" in args:
        show_prompts(_collect_all_exchanges, period, tool,
                     session_filter=_arg_value(args, "--session"))
    elif "--anomalies" in args:
        show_anomalies(_collect_all_exchanges, period, tool)
    elif "--activity" in args:
        show_activity(_collect_all_exchanges, period, tool)
    elif "--total" in args:
        show_total(_collect_all_exchanges, period, tool)
    elif "--impact" in args:
        show_impact(_collect_all_exchanges, period, tool, _parse_region(args))
    elif "--tool-use" in args:
        show_tool_use(_collect_all_exchanges, period, tool,
                      session_filter=_arg_value(args, "--session"))
    elif "--plan" in args:
        show_plan(_collect_all_exchanges, period, tool)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(_collect_all_exchanges, out, period, tool)
    else:
        main(period, tool, by_session=by_session)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
