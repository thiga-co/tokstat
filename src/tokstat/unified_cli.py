#!/usr/bin/env python3
"""
tokstat — Unified view of token consumption across all supported AI coding
assistants (Claude Code, Codex, Cursor, Kiro, Gemini CLI).

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import io
import json
import sys
import time
from contextlib import redirect_stdout
from datetime import datetime, timezone
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
    show_activity, show_total, show_impact, show_audit, show_bench,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    print_retention_alerts,
    compute_overview_state,
)


# Map each known tool name → (scanner, speed_scanner_or_None, collector, data_label)
_TOOLS = [
    ("Claude Code", scan_claude_code, scan_speed_claude_code, _collect_claude, "~/.claude/"),
    ("Codex",       scan_codex,       scan_speed_codex,       _collect_codex,  "~/.codex/"),
    ("Cursor",      scan_cursor,      None,                   _collect_cursor,
     "~/Library/.../Cursor/"),
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


# When a snapshot is loaded (--load), all scanning reads from it instead of the
# agents' on-disk data — tokstat then runs fully standalone/offline.
_SNAPSHOT: dict | None = None


def _scan_all(tool_filter: str | None) -> tuple[list[dict], list[dict], list[tuple[str, int, str]]]:
    """Run every registered scanner. Returns (records, speed_records, per_tool_counts)."""
    if _SNAPSHOT is not None:
        recs = [r for r in _SNAPSHOT["records"]
                if not tool_filter or r.get("tool") == tool_filter]
        spd = [s for s in _SNAPSHOT["speed_records"]
               if not tool_filter or s.get("tool") == tool_filter]
        counts = [(t, n, p) for (t, n, p) in _SNAPSHOT["counts"]
                  if not tool_filter or t == tool_filter]
        return recs, spd, counts

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
    if _SNAPSHOT is not None:
        exs = [e for e in _SNAPSHOT["exchanges"]
               if (not tool_filter or e.get("tool") == tool_filter)
               and e.get("ts") and e["ts"] >= cutoff
               and (cutoff_end is None or e["ts"] < cutoff_end)]
        counts: dict[str, int] = {}
        for e in exs:
            counts[e["tool"]] = counts.get(e["tool"], 0) + 1
        return exs, counts

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


# ─── Snapshot: --dump / --load ──────────────────────────────────────────────

_DUMP_VERSION = 2   # v2 adds per-exchange tool_outputs (captured tool result snippets)


def _iso(dt):
    return dt.isoformat() if dt else None


def _from_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None


def _today_midnight() -> datetime:
    """Local midnight today (tz-aware) — the boundary for --exclude-today."""
    return datetime.now().astimezone().replace(
        hour=0, minute=0, second=0, microsecond=0)


def _dump_snapshot(path: str, tool_filter: str | None = None,
                   exclude_today: bool = False) -> None:
    """Capture everything tokstat's analyses use — token records, output-speed
    records and full per-prompt exchanges (with text, tools, tokens, cost) — to
    a portable JSON file that `--load` can replay offline. With exclude_today,
    today's records/exchanges are left out of the snapshot."""
    from tokstat._core import BOLD, DIM, RESET, YELLOW
    print(f"\n{BOLD} Dumping tokstat snapshot{RESET}")
    print(f"{DIM}  Scanning all data sources"
          f"{' (excluding today)' if exclude_today else ' (full history)'}...{RESET}\n")

    load_pricing()   # so per-record cost is computed and captured in the dump
    records, speed_records, counts = _scan_all(tool_filter)
    epoch = datetime.min.replace(tzinfo=timezone.utc)
    end = _today_midnight() if exclude_today else None
    exchanges, _ = _collect_all_exchanges(epoch, tool_filter, end)
    if exclude_today:
        records = [r for r in records if not r.get("ts") or r["ts"] < end]
        speed_records = [s for s in speed_records if not s.get("ts") or s["ts"] < end]
        counts = [(t, sum(1 for r in records if r.get("tool") == t), p)
                  for (t, _n, p) in counts]

    def _ser_record(r):
        d = dict(r)
        d["ts"] = _iso(r.get("ts"))
        return d

    def _ser_exchange(e):
        d = dict(e)
        d["ts"] = _iso(e.get("ts"))
        if e.get("tools_used") is not None:
            d["tools_used"] = dict(e["tools_used"])   # defaultdict → dict
        return d

    snapshot = {
        "tokstat_dump": _DUMP_VERSION,
        "version": __version__,
        "dumped_at": None,  # stamped by the OS mtime; avoid Date.now-style calls
        "records": [_ser_record(r) for r in records],
        "speed_records": [_ser_record(s) for s in speed_records],
        "exchanges": [_ser_exchange(e) for e in exchanges],
        "counts": [list(c) for c in counts],
    }
    out = Path(path)
    out.write_text(json.dumps(snapshot, ensure_ascii=False))
    size_mb = out.stat().st_size / (1024 * 1024)
    if not records and not exchanges:
        print(f"  {YELLOW}No data found to dump.{RESET}\n")
    for tool_name, n, _p in counts:
        if n:
            color = TOOL_COLORS.get(tool_name, "")
            print(f"  {color}●{RESET} {tool_name:<12} {n:>6} records")
    print(f"\n  {BOLD}{len(exchanges)}{RESET} exchanges, "
          f"{BOLD}{len(records)}{RESET} records → {BOLD}{path}{RESET} "
          f"{DIM}({size_mb:.1f} MB){RESET}")
    print(f"  {DIM}Replay offline with: tokstat --load {path} [any mode]{RESET}\n")


def _load_snapshot(path: str) -> bool:
    """Load a snapshot so all scanning reads from it (standalone/offline).
    Returns True on success."""
    global _SNAPSHOT
    from tokstat._core import RED, YELLOW, DIM, RESET, BOLD
    p = Path(path)
    if not p.exists():
        print(f"\n  {RED}Snapshot not found: {path}{RESET}\n")
        return False
    try:
        data = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"\n  {RED}Could not read snapshot {path}: {e}{RESET}\n")
        return False
    if not isinstance(data, dict) or "tokstat_dump" not in data:
        print(f"\n  {RED}{path} is not a tokstat snapshot.{RESET}\n")
        return False

    for r in data.get("records", []):
        r["ts"] = _from_iso(r.get("ts"))
    for s in data.get("speed_records", []):
        s["ts"] = _from_iso(s.get("ts"))
    for e in data.get("exchanges", []):
        e["ts"] = _from_iso(e.get("ts"))
    _SNAPSHOT = {
        "records": data.get("records", []),
        "speed_records": data.get("speed_records", []),
        "exchanges": data.get("exchanges", []),
        "counts": [tuple(c) for c in data.get("counts", [])],
    }
    n_ex = len(_SNAPSHOT["exchanges"])
    print(f"{DIM}  Loaded snapshot {BOLD}{path}{RESET}{DIM} "
          f"(v{data.get('version', '?')}, {n_ex} exchanges) — "
          f"standalone mode, not reading agent data.{RESET}")
    return True


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
        tool_ts = [r["ts"] for r in records if r.get("tool") == tool_name]
        n_in_period = len(tool_ts)
        color = TOOL_COLORS.get(tool_name, "")
        span = f"{DIM}{_span_label(tool_ts):>10}{RESET}"
        since = (f"{DIM}since {min(tool_ts).strftime('%Y-%m-%d')}{RESET}"
                 if tool_ts else f"{DIM}{'—':>16}{RESET}")
        print(f"  {color}●{RESET} {tool_name:<12} {n_in_period:>6} records · "
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
    "--plan", "--activity", "--total", "--impact", "--audit", "--judge",
    "--model", "--judge-max", "--dump", "--load", "--bench", "--exclude-today",
    "--verify", "--claude-judge", "--claude-model",
    "--export", "--period", "--since", "--tool", "--watch", "-w",
}


def _arg_value(args, flag, default=None):
    """Return the value following `flag`, or default."""
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return default


def _model_list(args):
    """Parse --model as a comma/space-separated list, tolerating stray spaces
    (e.g. "a,b ,c" or "a b c"). Consumes every token after --model up to the
    next flag, then splits on commas/whitespace. Returns a comma-joined string
    (for a single judge model or a panel), or None."""
    if "--model" not in args:
        return None
    i = args.index("--model")
    parts = []
    for a in args[i + 1:]:
        if a.startswith("-"):
            break
        parts.append(a)
    models = [m for m in " ".join(parts).replace(",", " ").split() if m]
    # de-dup, preserve order
    models = list(dict.fromkeys(models))
    return ",".join(models) if models else None

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
  tokstat --activity                       Activity calendar (GitHub-style, by day)
  tokstat --total                          Compact totals (tokens + cost + data span)
  tokstat --audit                          Conversation quality audit — all 12
                                           metrics via LOCAL Ollama model(s)
                                           (nothing leaves the machine).
                                           --model a,b,c = judge panel (votes);
                                           --verify = skeptical 2nd pass (cuts
                                           false positives);
                                           --claude-judge = add a stronger
                                           Claude-CLI "juge de paix" (sends
                                           excerpts off-machine to the Claude
                                           API; --claude-model <m> to pick it);
                                           [--model <name(s)>] [--judge-max <n>]
  tokstat --impact [region]                Energy & CO₂ estimate (EcoLogits;
                                           region: world/eu/france/us/green)
  tokstat --plan                           Cost breakdown + optimization tips
  tokstat --export   [file.json]           Export all exchanges to JSON
  tokstat --bench    [--model a,b,c]       Benchmark judge model speed (prefill/decode tok/s)
  tokstat --dump     [file.json]           Capture ALL data to a portable snapshot
  tokstat --load <file> <mode>             Run any mode from a snapshot, offline
                                           (does not read the agents' data)
  tokstat --watch    [-w] [SECONDS]        Refresh overview live (default 5s, Ctrl+C to stop)
  tokstat --version  [-V]                  Show version
  tokstat --help     [-h]                  This help

{BOLD}FILTERS{RESET}
  --period <period>    all, today, yesterday, year, or any "N unit" —
                       e.g. "12 hours", "5 days", "31 days", "2 weeks",
                       "3 months"   (partial match works; default: today)
  --tool   <name>      claude, codex, cursor, kiro, gemini, opencode,
                       claude.ai, chatgpt (default: all)
  --exclude-today      drop today's conversations (skip in-progress / meta
                       sessions) — handy for --audit on a dump

{BOLD}TOOLS COVERED{RESET}
  Claude Code  ~/.claude/projects/                          exact tokens
  Codex        ~/.codex/sessions/                           exact tokens
  Cursor       Cursor globalStorage/state.vscdb             exact / no data
  Kiro         Kiro .../workspace-sessions/                 activity only
  Gemini CLI   ~/.gemini/tmp/                               exact tokens
  opencode     ~/.local/share/opencode/storage/             exact tokens
  Claude.ai    --import of official export (claude-web-token-usage)
  ChatGPT      --import of official export (chatgpt-web-token-usage)

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

    # --load: read from a snapshot instead of the agents' data (standalone).
    if "--load" in args:
        snap = _arg_value(args, "--load")
        if not snap:
            print(f"\n  {RED}--load needs a snapshot file: --load <file>{RESET}\n")
            sys.exit(1)
        if not _load_snapshot(snap):
            sys.exit(1)

    # --dump: capture everything to a snapshot file, then stop.
    if "--dump" in args:
        out = _arg_value(args, "--dump") or "tokstat-dump.json"
        _dump_snapshot(out, tool, exclude_today="--exclude-today" in args)
        return

    # --bench: measure the local judge model(s) speed on this machine.
    if "--bench" in args:
        show_bench(_model_list(args))
        return

    # --exclude-today: clamp the period end to local midnight so today's
    # (often in-progress / meta) conversations don't skew an audit of a dump.
    collect = _collect_all_exchanges
    if "--exclude-today" in args:
        def collect(cutoff, tool_filter=None, cutoff_end=None,
                    _c=_collect_all_exchanges):
            midnight = _today_midnight()
            end = midnight if cutoff_end is None else min(cutoff_end, midnight)
            return _c(cutoff, tool_filter, end)

    watch_interval = _parse_watch_interval(args)
    if watch_interval is not None:
        if any(f in args for f in ("--prompts", "-p", "--anomalies", "--plan", "--export")):
            print(f"\n  {RED}--watch only applies to the default overview mode.{RESET}\n")
            sys.exit(1)
        watch(period, tool, watch_interval)
        return

    if "--prompts" in args or "-p" in args:
        show_prompts(collect, period, tool)
    elif "--anomalies" in args:
        show_anomalies(collect, period, tool)
    elif "--activity" in args:
        show_activity(collect, period, tool)
    elif "--total" in args:
        show_total(collect, period, tool)
    elif "--impact" in args:
        show_impact(collect, period, tool, _parse_region(args))
    elif "--audit" in args:
        jmax_raw = _arg_value(args, "--judge-max")   # default: no cap
        try:
            jmax = int(jmax_raw) if jmax_raw is not None else None
        except ValueError:
            jmax = None
        show_audit(collect, period, tool,
                   judge_model=_model_list(args), judge_max=jmax,
                   verify="--verify" in args,
                   claude_judge="--claude-judge" in args,
                   claude_model=_arg_value(args, "--claude-model"))
    elif "--plan" in args:
        show_plan(collect, period, tool)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(collect, out, period, tool)
    else:
        main(period, tool)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
