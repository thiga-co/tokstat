#!/usr/bin/env python3
"""
copilot-cli-token-usage — Token consumption from the GitHub Copilot CLI.

Scans the Copilot CLI's local store (`~/.copilot/session-store.db`). Unlike the
Copilot **Chat** extension (activity only — see copilot_cli.py), the Copilot
**CLI** records **exact** token usage in `assistant_usage_events`
(input/output/cache read+write/reasoning, TTFT and duration per request), the
working directory per session, the conversation turns, and the tool/file
timeline — so it supports the full feature set.

This is a separate product and a separate store from the Copilot Chat extension
(`…/globalStorage/github.copilot-chat/session-store.db`) and VS Code's built-in
chat; running Copilot from a terminal (incl. VS Code's integrated agent) lands
here.

Token note: `input_tokens` is the full prompt and already includes
`cache_read_tokens`, so fresh input = input − cache_read; `cache_write_tokens`
is separate. (Copilot's own model names, e.g. `mai-code-1.1-flash`, are usually
absent from LiteLLM, so cost shows $0 while token counts stay exact.)

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import sqlite3
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from tokstat.cli import __version__

from tokstat._core import (
    BOLD, DIM, RESET, RED, YELLOW,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact, show_tool_use,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    project_matches, session_matches,
)

TOOL_NAME = "Copilot CLI"
TOOL_COLORS[TOOL_NAME] = "\033[38;5;208m"   # orange, distinct from Copilot Chat

COPILOT_CLI_DB = Path.home() / ".copilot" / "session-store.db"


def _connect() -> sqlite3.Connection | None:
    if not COPILOT_CLI_DB.exists():
        return None
    try:
        return sqlite3.connect(f"file:{COPILOT_CLI_DB}?mode=ro", uri=True)
    except sqlite3.Error:
        return None


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _event_tokens(inp, out, cr, cw) -> dict:
    """input_tokens includes cache_read → subtract; cache_write is separate."""
    inp = inp or 0
    cr = cr or 0
    return {
        "input":       max(inp - cr, 0),
        "output":      out or 0,
        "cache_read":  cr,
        "cache_write": cw or 0,
    }


def _load():
    """Read the Copilot CLI store once and return the pieces the scanners share:
    sessions, per-(session,turn) usage aggregates, turn text, and tool calls."""
    con = _connect()
    if con is None:
        return None
    try:
        sessions: dict[str, dict] = {}
        try:
            for sid, cwd, summary, created in con.execute(
                    "SELECT id, cwd, summary, created_at FROM sessions"):
                sessions[sid] = {"cwd": cwd or "unknown",
                                 "summary": (summary or "").strip(),
                                 "created": created}
        except sqlite3.Error:
            pass

        # Usage events — one row each (one API request).
        events: list[dict] = []
        try:
            for (sid, ti, model, inp, out, cr, cw, reasoning,
                 dur, ttft, created) in con.execute(
                    "SELECT session_id, turn_index, model, input_tokens, "
                    "output_tokens, cache_read_tokens, cache_write_tokens, "
                    "reasoning_tokens, duration_ms, time_to_first_token_ms, "
                    "created_at FROM assistant_usage_events ORDER BY id"):
                tk = _event_tokens(inp, out, cr, cw)
                events.append({
                    "sid": sid, "ti": ti if ti is not None else 0,
                    "model": model or "copilot-cli", "tokens": tk,
                    "cost": compute_cost(tk, model or ""),
                    "duration_ms": dur or 0, "ttft_ms": ttft,
                    "ts": _parse_ts(created),
                })
        except sqlite3.Error:
            pass

        # Turn text.
        turns: dict[tuple, dict] = {}
        try:
            for sid, ti, user_msg, assistant, ts_str in con.execute(
                    "SELECT session_id, turn_index, user_message, "
                    "assistant_response, timestamp FROM turns"):
                turns[(sid, ti if ti is not None else 0)] = {
                    "user": (user_msg or "").strip(),
                    "assistant": (assistant or "").strip(),
                    "ts": _parse_ts(ts_str),
                }
        except sqlite3.Error:
            pass

        # Tool calls: per-turn when turn_index is set, else session-level.
        tools_by_turn: dict[tuple, list[dict]] = defaultdict(list)
        tools_by_session: dict[str, list[dict]] = defaultdict(list)
        try:
            for sid, ti, tool_name, fpath in con.execute(
                    "SELECT session_id, turn_index, tool_name, file_path "
                    "FROM session_files ORDER BY turn_index, first_seen_at"):
                call = {"name": tool_name or "tool", "target": fpath or "",
                        "error": False}
                if ti is None:
                    tools_by_session[sid].append(call)
                else:
                    tools_by_turn[(sid, ti)].append(call)
        except sqlite3.Error:
            pass

        return sessions, events, turns, tools_by_turn, tools_by_session
    finally:
        con.close()


def _usage_by_turn(events: list[dict]) -> dict[tuple, dict]:
    """Aggregate raw usage events per (session, turn_index)."""
    usage: dict[tuple, dict] = {}
    for e in events:
        key = (e["sid"], e["ti"])
        agg = usage.get(key)
        if agg is None:
            agg = {"model": e["model"], "n": 0,
                   "tokens": {"input": 0, "output": 0,
                              "cache_read": 0, "cache_write": 0},
                   "cost": 0.0, "duration_ms": 0, "ttft_ms": None, "first_ts": None}
            usage[key] = agg
        for k in agg["tokens"]:
            agg["tokens"][k] += e["tokens"][k]
        agg["cost"] += e["cost"]
        agg["n"] += 1
        agg["duration_ms"] += e["duration_ms"]
        if agg["ttft_ms"] is None and e["ttft_ms"]:
            agg["ttft_ms"] = e["ttft_ms"]
        if e["ts"] and (agg["first_ts"] is None or e["ts"] < agg["first_ts"]):
            agg["first_ts"] = e["ts"]
    return usage


def _exchanges() -> list[dict]:
    """One exchange per (session, turn): user/assistant text from `turns`,
    tokens/cost/ttft from `assistant_usage_events`, tool calls from
    `session_files`. Covers sessions that have usage events even when the turns
    table wasn't populated (summary used as the prompt label)."""
    loaded = _load()
    if loaded is None:
        return []
    sessions, events, turns, tools_by_turn, tools_by_session = loaded
    usage = _usage_by_turn(events)

    keys = set(usage.keys()) | set(turns.keys())
    # which turn is first in each session (to attach session-level tools + summary)
    first_ti: dict[str, int] = {}
    for sid, ti in keys:
        if sid not in first_ti or ti < first_ti[sid]:
            first_ti[sid] = ti

    exchanges: list[dict] = []
    for (sid, ti) in sorted(keys):
        sess = sessions.get(sid, {"cwd": "unknown", "summary": "", "created": None})
        u = usage.get((sid, ti))
        t = turns.get((sid, ti))

        ts = (t and t["ts"]) or (u and u["first_ts"]) or _parse_ts(sess.get("created"))
        if ts is None:
            continue

        user_text = (t["user"] if t and t["user"]
                     else (sess["summary"] if ti == first_ti.get(sid) else ""))
        assistant_texts = [t["assistant"]] if t and t["assistant"] else []

        calls = list(tools_by_turn.get((sid, ti), []))
        if ti == first_ti.get(sid):
            calls = tools_by_session.get(sid, []) + calls
        for c in calls:
            c["ts"] = ts
        tools_used: dict[str, int] = defaultdict(int)
        for c in calls:
            tools_used[c["name"]] += 1

        tokens = (u["tokens"] if u else
                  {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0})
        ex = {
            "session_id":      sid,
            "user_text":       user_text,
            "assistant_texts": assistant_texts,
            "tool_errors":     [],
            "tools_used":      tools_used,
            "tool_calls":      calls,
            "num_turns":       (u["n"] if u else 1),
            "model":           (u["model"] if u else "copilot-cli"),
            "project":         sess["cwd"],
            "ts":              ts,
            "tokens":          tokens,
            "cost":            (u["cost"] if u else 0.0),
            "context_peak":    tokens["input"] + tokens["cache_read"] + tokens["cache_write"],
        }
        if u and u["ttft_ms"]:
            ex["ttft_s"] = u["ttft_ms"] / 1000.0
        if u and u["duration_ms"]:
            ex["duration_s"] = u["duration_ms"] / 1000.0
        exchanges.append(ex)
    return exchanges


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_copilotcli() -> list[dict]:
    """One token record per assistant usage event (= one API request)."""
    records = []
    loaded = _load()
    if loaded is None:
        return records
    sessions, events, _turns, _tbt, _tbs = loaded
    for e in events:
        ts = e["ts"]
        if ts is None:
            continue
        sess = sessions.get(e["sid"], {"cwd": "unknown"})
        records.append({
            "tool":        TOOL_NAME,
            "model":       e["model"],
            "project":     sess["cwd"],
            "ts":          ts,
            "session_id":  e["sid"],
            **e["tokens"],
            "cost":        e["cost"],
        })
    return records


def scan_speed_copilotcli() -> list[dict]:
    """Exact output speed per request from duration_ms + output tokens, with
    TTFT from time_to_first_token_ms."""
    results = []
    loaded = _load()
    if loaded is None:
        return results
    sessions, events, _turns, _tbt, _tbs = loaded
    for e in events:
        ts = e["ts"]
        out = e["tokens"]["output"]
        dur_ms = e["duration_ms"]
        if ts is None or out < 10 or dur_ms <= 0:
            continue
        dt = dur_ms / 1000.0
        speed = out / dt
        if speed > 2000:
            continue
        sess = sessions.get(e["sid"], {"cwd": "unknown"})
        results.append({
            "tool":       TOOL_NAME,
            "model":      e["model"],
            "project":    sess["cwd"],
            "ts":         ts,
            "session_id": e["sid"],
            "tokens":     out,
            "duration":   dt,
            "speed":      speed,
            "ttft":       (e["ttft_ms"] / 1000.0) if e["ttft_ms"] else None,
        })
    return results


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    all_exchanges: list[dict] = []
    tool_counts: dict[str, int] = {}
    if tool_filter and tool_filter != TOOL_NAME:
        return all_exchanges, tool_counts
    filtered = [ex for ex in _exchanges()
                if ex["ts"] and ex["ts"] >= cutoff
                and (cutoff_end is None or ex["ts"] < cutoff_end)]
    for ex in filtered:
        ex["tool"] = TOOL_NAME
    if filtered:
        all_exchanges.extend(filtered)
        tool_counts[TOOL_NAME] = len(filtered)
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None,
         by_session: bool = False, by_project: bool = False,
         project_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — {TOOL_NAME}{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.copilot/session-store.db...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not COPILOT_CLI_DB.exists():
        print(f"  {DIM}Copilot CLI not found at ~/.copilot/session-store.db{RESET}\n")
        return

    records = scan_copilotcli()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]
    if project_filter:
        records = [r for r in records if project_matches(r.get("project"), project_filter)]

    color = TOOL_COLORS[TOOL_NAME]
    if records:
        print(f"  {color}●{RESET} {TOOL_NAME:<12} {len(records):>6} records from ~/.copilot/")
    print()
    print(f"  Period: {BOLD}{period_label}{RESET}")
    if project_filter:
        print(f"  Project: {BOLD}{project_filter}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    speed_records = scan_speed_copilotcli()
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges,
                         by_session=by_session, by_project=by_project,
                         project_filter=project_filter)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"copilot-cli": TOOL_NAME, "copilotcli": TOOL_NAME,
                 "gh-copilot-cli": TOOL_NAME}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--activity", "--total", "--impact", "--by-session", "--by-project",
    "--tool-use", "--session", "--project", "--export", "--period", "--since", "--tool",
}


def _arg_value(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return default


def _parse_tool(args: list[str]) -> str | None:
    if "--tool" not in args:
        return None
    idx = args.index("--tool")
    if idx + 1 >= len(args):
        return None
    raw = args[idx + 1].lower().strip()
    if raw in ("all", "tous", "*"):
        return None
    if raw in _TOOL_ALIASES:
        return _TOOL_ALIASES[raw]
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: copilot-cli")


def show_help():
    print(f"""
{BOLD}copilot-cli-token-usage{RESET} — Aggregate and analyze GitHub Copilot CLI token consumption.

{BOLD}MODES{RESET}
  copilot-cli-token-usage                       Aggregated overview (period, project, model, speed)
  copilot-cli-token-usage --prompts  [-p]       Per-exchange detail (text, model, turns, tokens)
  copilot-cli-token-usage --anomalies           Technical anomaly detection
  copilot-cli-token-usage --activity            Activity calendar (GitHub-style, by day)
  copilot-cli-token-usage --total               Compact totals (tokens + cost + data span)
  copilot-cli-token-usage --impact              Energy & CO₂ estimate (EcoLogits)
  copilot-cli-token-usage --by-session          Overview + per-session table
  copilot-cli-token-usage --by-project          Overview + a project → sessions drill-down
  copilot-cli-token-usage --tool-use            Timeline of tool calls (file + time)
  copilot-cli-token-usage --plan                Cost breakdown + plan recommendation
  copilot-cli-token-usage --export [file.json]  Export all exchanges to JSON
  copilot-cli-token-usage --help     [-h]       This help

{BOLD}FILTERS{RESET}  {DIM}(apply to all modes){RESET}
  --period <period>      today, yesterday, "7 days", "30 days", all, …
  --project <name>       scope to one project (substring on the repo path)
  --session <id>         scope to one session (id or short handle)

{BOLD}DATA SOURCE{RESET}
  {TOOL_COLORS[TOOL_NAME]}Copilot CLI{RESET}    {DIM}~/.copilot/session-store.db{RESET}    ✓ Tokens ✓ Text ✓ Tools ✓ Speed

  {DIM}Separate from the Copilot Chat extension (activity only). Copilot's own model
  names are usually absent from LiteLLM, so token counts are exact but cost shows $0.{RESET}
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
        print(f"  Run {BOLD}copilot-cli-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)
    try:
        tool = _parse_tool(args)
    except ValueError as e:
        print(f"\n  {RED}{e}{RESET}\n")
        sys.exit(1)

    project = _arg_value(args, "--project")
    session = _arg_value(args, "--session")

    if "--prompts" in args or "-p" in args:
        show_prompts(_collect_all_exchanges, period, tool,
                     session_filter=session, project_filter=project)
    elif "--anomalies" in args:
        show_anomalies(_collect_all_exchanges, period, tool,
                       project_filter=project, session_filter=session)
    elif "--activity" in args:
        show_activity(_collect_all_exchanges, period, tool,
                      project_filter=project, session_filter=session)
    elif "--total" in args:
        show_total(_collect_all_exchanges, period, tool,
                   project_filter=project, session_filter=session)
    elif "--impact" in args:
        show_impact(_collect_all_exchanges, period, tool, _parse_region(args),
                    project_filter=project, session_filter=session)
    elif "--tool-use" in args:
        show_tool_use(_collect_all_exchanges, period, tool,
                      session_filter=session, project_filter=project)
    elif "--plan" in args:
        show_plan(_collect_all_exchanges, period, tool,
                  project_filter=project, session_filter=session)
    elif "--export" in args:
        idx = args.index("--export")
        out = "conversations.json"
        if idx + 1 < len(args) and not args[idx + 1].startswith("--"):
            out = args[idx + 1]
        export_conversations(_collect_all_exchanges, out, period, tool,
                             project_filter=project, session_filter=session)
    else:
        main(period, tool, by_session="--by-session" in args,
             by_project="--by-project" in args, project_filter=project)

    print_update_notice(__version__)


if __name__ == "__main__":
    cli()
