#!/usr/bin/env python3
"""
pi-token-usage — Aggregate and display token consumption from the pi coding agent.

Scans ~/.pi/agent/sessions/ JSONL transcripts (the open-source `pi` harness,
@earendil-works/pi-coding-agent) to extract token usage and estimate costs.

pi records exact token counts, model, provider, tool calls, duration and the
working directory per session — so, unlike activity-only tools, it supports the
full feature set (overview, --prompts, --tool-use, --impact, --plan, …). Local
models (e.g. Ollama) carry real token counts but no price, so they correctly
show $0.00 rather than "no data".

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

from tokstat.cli import __version__

import json
import sys
from datetime import datetime
from pathlib import Path

from tokstat._core import (
    BOLD, DIM, RESET, GREEN, YELLOW, RED, WHITE,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact, show_tool_use, tool_target,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    project_matches,
)

# Register pi color
TOOL_COLORS["pi"] = WHITE

PI_SESSIONS = Path.home() / ".pi" / "agent" / "sessions"


# ─── Shared helpers ────────────────────────────────────────────────────────────

def _ts(rec: dict):
    """pi stamps every top-level record with an ISO-8601 `timestamp`
    (the inner `.message.timestamp` is an unreliable epoch value — ignore it)."""
    s = rec.get("timestamp")
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _label(model: str | None, thinking: str | None) -> str:
    """Display name for a pi turn. Mirrors Codex's `[effort]` convention so
    token rows and prompt/turn rows land on one model line; `medium` is the
    default thinking level and is left implicit."""
    m = model or "pi-unknown"
    if thinking and thinking != "medium":
        m = f"{m} [{thinking}]"
    return m


def _usage_tokens(usage: dict) -> dict:
    """Normalize pi's usage block to tokstat's token dict."""
    return {
        "input":       usage.get("input", 0) or 0,
        "output":      usage.get("output", 0) or 0,
        "cache_read":  usage.get("cacheRead", 0) or 0,
        "cache_write": usage.get("cacheWrite", 0) or 0,
    }


def _iter_lines(jsonl_file: Path):
    """Yield parsed JSON objects from a pi transcript, skipping bad lines."""
    with open(jsonl_file, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_pi() -> list[dict]:
    """Scan pi session JSONL files for token usage.

    One record per assistant message carrying a usage block. Model/provider and
    thinking level are stamped on each assistant message; cwd and the session id
    come from the leading `session` record.
    """
    records: list[dict] = []
    if not PI_SESSIONS.exists():
        return records
    for jsonl_file in PI_SESSIONS.rglob("*.jsonl"):
        project = "unknown"
        session_id = jsonl_file.stem
        current_model = "pi-unknown"
        current_thinking = ""
        try:
            for rec in _iter_lines(jsonl_file):
                rtype = rec.get("type")
                if rtype == "session":
                    project = rec.get("cwd", project) or project
                    session_id = rec.get("id", session_id) or session_id
                    continue
                if rtype == "model_change":
                    if rec.get("modelId"):
                        current_model = rec["modelId"]
                    continue
                if rtype == "thinking_level_change":
                    current_thinking = rec.get("thinkingLevel", "") or ""
                    continue
                if rtype != "message":
                    continue
                msg = rec.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                usage = msg.get("usage") or {}
                if not usage:
                    continue
                tokens = _usage_tokens(usage)
                if not any(tokens.values()):
                    continue
                ts = _ts(rec)
                if ts is None:
                    continue
                model = _label(msg.get("model") or current_model,
                               msg.get("thinkingLevel") or current_thinking)
                records.append({
                    "tool":       "pi",
                    "model":      model,
                    "project":    project,
                    "ts":         ts,
                    "session_id": session_id,
                    **tokens,
                    "cost":       compute_cost(tokens, model),
                })
        except (OSError, IOError):
            continue
    return records


def scan_speed_pi() -> list[dict]:
    """Extract output speed (tokens/sec) from pi transcripts.

    pi records `durationMs` (generation time) on each assistant message, so the
    rate is exact — no heuristic timing needed.
    """
    results: list[dict] = []
    if not PI_SESSIONS.exists():
        return results
    for jsonl_file in PI_SESSIONS.rglob("*.jsonl"):
        project = "unknown"
        session_id = jsonl_file.stem
        current_model = "pi-unknown"
        current_thinking = ""
        try:
            for rec in _iter_lines(jsonl_file):
                rtype = rec.get("type")
                if rtype == "session":
                    project = rec.get("cwd", project) or project
                    session_id = rec.get("id", session_id) or session_id
                    continue
                if rtype == "model_change":
                    if rec.get("modelId"):
                        current_model = rec["modelId"]
                    continue
                if rtype == "thinking_level_change":
                    current_thinking = rec.get("thinkingLevel", "") or ""
                    continue
                if rtype != "message":
                    continue
                msg = rec.get("message") or {}
                if msg.get("role") != "assistant":
                    continue
                dur_ms = msg.get("durationMs") or 0
                out = (msg.get("usage") or {}).get("output", 0) or 0
                if dur_ms <= 0 or out < 10:
                    continue
                dt = dur_ms / 1000.0
                speed = out / dt
                if speed > 2000:          # guard against clock artifacts
                    continue
                ts = _ts(rec)
                if ts is None:
                    continue
                model = _label(msg.get("model") or current_model,
                               msg.get("thinkingLevel") or current_thinking)
                results.append({
                    "tool":       "pi",
                    "model":      model,
                    "project":    project,
                    "ts":         ts,
                    "session_id": session_id,
                    "tokens":     out,
                    "duration":   dt,
                    "speed":      speed,
                })
        except (OSError, IOError):
            continue
    return results


# ─── Exchanges ─────────────────────────────────────────────────────────────────

def _user_text(content) -> str:
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = [c.get("text", "") for c in content
                 if isinstance(c, dict) and c.get("type") == "text"]
        return "\n".join(p for p in parts if p).strip()
    return ""


def _result_snippet(content) -> str:
    if isinstance(content, str):
        return " ".join(content.split())[:200]
    if isinstance(content, list):
        for c in content:
            if isinstance(c, dict) and c.get("text"):
                return " ".join(str(c["text"]).split())[:200]
    return ""


def _extract_exchanges_pi(jsonl_path: str) -> list[dict]:
    """Parse one pi transcript into exchanges (one per user prompt)."""
    try:
        lines = list(_iter_lines(Path(jsonl_path)))
    except (OSError, IOError):
        return []

    exchanges: list[dict] = []
    current = None
    current_model = "pi-unknown"
    current_thinking = ""
    current_cwd = None
    session_id = Path(jsonl_path).stem
    calls_by_id: dict = {}

    def _new(user_text, ts):
        return {
            "user_text": user_text, "assistant_texts": [], "tool_errors": [],
            "tools_used": {}, "num_turns": 0,
            "model": _label(current_model, current_thinking),
            "project": current_cwd, "ts": ts, "last_ts": ts,
            "first_assistant_ts": None,
            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
            "cost": 0.0,
            "context_peak": 0, "context_first": None, "context_last": 0,
            "compactions": [],
            "tool_calls": [],
            "session_id": session_id,
        }

    for rec in lines:
        rtype = rec.get("type")

        if rtype == "session":
            if rec.get("cwd"):
                current_cwd = rec["cwd"]
            if rec.get("id"):
                session_id = rec["id"]
            continue
        if rtype == "model_change":
            if rec.get("modelId"):
                current_model = rec["modelId"]
            continue
        if rtype == "thinking_level_change":
            current_thinking = rec.get("thinkingLevel", "") or ""
            continue
        if rtype != "message":
            continue

        msg = rec.get("message") or {}
        role = msg.get("role")
        ts = _ts(rec)

        if role == "user":
            if current:
                exchanges.append(current)
            calls_by_id = {}
            current = _new(_user_text(msg.get("content")), ts)

        elif role == "assistant":
            if current is None:
                current = _new("", ts)
                calls_by_id = {}
            if ts:
                current["last_ts"] = ts
                if current["first_assistant_ts"] is None:
                    current["first_assistant_ts"] = ts
            current["num_turns"] += 1
            if msg.get("model"):
                current_model = msg["model"]
            if msg.get("thinkingLevel"):
                current_thinking = msg["thinkingLevel"]
            current["model"] = _label(current_model, current_thinking)

            usage = msg.get("usage") or {}
            if usage:
                tk = _usage_tokens(usage)
                for key in ("input", "output", "cache_read", "cache_write"):
                    current["tokens"][key] += tk[key]
                current["cost"] += compute_cost(tk, current["model"])
                ctx = tk["input"] + tk["cache_read"] + tk["cache_write"]
                if ctx > 0:
                    if ctx > current["context_peak"]:
                        current["context_peak"] = ctx
                    if current["context_first"] is None:
                        current["context_first"] = ctx
                    current["context_last"] = ctx

            for c in msg.get("content", []) or []:
                if not isinstance(c, dict):
                    continue
                ctype = c.get("type")
                if ctype == "text":
                    t = (c.get("text") or "").strip()
                    if t:
                        current["assistant_texts"].append(t)
                elif ctype == "toolCall":
                    name = c.get("name") or "?"
                    args = c.get("arguments") if isinstance(c.get("arguments"), dict) else {}
                    call = {"ts": ts, "name": name,
                            "target": tool_target(name, args), "error": False,
                            "id": c.get("id")}
                    current["tool_calls"].append(call)
                    current["tools_used"][name] = current["tools_used"].get(name, 0) + 1
                    if c.get("id"):
                        calls_by_id[c["id"]] = call

        elif role == "toolResult":
            if current is None:
                continue
            if ts:
                current["last_ts"] = ts
            if msg.get("isError"):
                call = calls_by_id.get(msg.get("toolCallId"))
                if call:
                    call["error"] = True
                snippet = _result_snippet(msg.get("content"))
                current["tool_errors"].append(
                    f"{msg.get('toolName') or 'tool'}: {snippet}".strip())

        # role == "system" and anything else: ignored

    if current:
        exchanges.append(current)

    for ex in exchanges:
        start, end = ex.get("ts"), ex.pop("last_ts", None)
        if start and end:
            ex["duration_s"] = max((end - start).total_seconds(), 0.0)
        first_a = ex.pop("first_assistant_ts", None)
        if start and first_a:
            ex["ttft_s"] = max((first_a - start).total_seconds(), 0.0)
        first = ex.pop("context_first", None)
        if first is not None:
            ex["context_growth"] = max(ex.get("context_last", 0) - first, 0)
        ex.pop("context_last", None)
    return exchanges


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Collect pi exchanges filtered by time."""
    all_exchanges: list[dict] = []
    tool_counts: dict[str, int] = {}

    if tool_filter and tool_filter != "pi":
        return all_exchanges, tool_counts

    if PI_SESSIONS.exists():
        for jsonl_file in PI_SESSIONS.rglob("*.jsonl"):
            exs = _extract_exchanges_pi(str(jsonl_file))
            filtered = [ex for ex in exs
                        if ex["ts"] and ex["ts"] >= cutoff
                        and (cutoff_end is None or ex["ts"] < cutoff_end)]
            for ex in filtered:
                ex["tool"] = "pi"
            if filtered:
                all_exchanges.extend(filtered)
                tool_counts["pi"] = tool_counts.get("pi", 0) + len(filtered)

    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None,
         by_session: bool = False, by_project: bool = False,
         project_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — pi{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.pi/agent/sessions/...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not PI_SESSIONS.exists():
        print(f"  {DIM}pi not found at ~/.pi/agent/sessions/{RESET}\n")
        return

    records = scan_pi()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]
    if project_filter:
        records = [r for r in records if project_matches(r.get("project"), project_filter)]

    if records:
        print(f"  {WHITE}●{RESET} {'pi':<12} {len(records):>6} records from ~/.pi/agent/sessions/")
    print()
    print(f"  Period: {BOLD}{period_label}{RESET}")
    if project_filter:
        print(f"  Project: {BOLD}{project_filter}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    speed_records = scan_speed_pi()
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges,
                         by_session=by_session, by_project=by_project,
                         project_filter=project_filter)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"pi": "pi"}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--activity", "--total", "--impact", "--by-session", "--by-project",
    "--tool-use", "--session", "--project", "--export", "--period", "--since", "--tool",
}


def _arg_value(args, flag, default=None):
    """Value following `flag` on the command line, or default."""
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
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: pi")


def show_help():
    print(f"""
{BOLD}pi-token-usage{RESET} — Aggregate and analyze pi (coding agent) token consumption.

{BOLD}MODES{RESET}
  pi-token-usage                            Aggregated overview (period, project, model, speed)
  pi-token-usage --prompts  [-p]            Per-exchange detail (text, model, turns, tokens)
  pi-token-usage --anomalies                Technical anomaly detection (cost, cache, tool storms)
  pi-token-usage --activity                 Activity calendar (GitHub-style, by day)
  pi-token-usage --total                    Compact totals (tokens + cost + data span)
  pi-token-usage --impact                   Energy & CO₂ estimate (EcoLogits)
  pi-token-usage --by-session               Overview + per-session table (all sessions)
  pi-token-usage --by-project               Overview + a project → sessions drill-down
  pi-token-usage --project <name>           Scope every view to one project (substring)
  pi-token-usage --session <id>             Scope every view to one session (id or short handle)
  pi-token-usage --tool-use                 Timeline of tool calls (file/command + time)
  pi-token-usage --plan                     Cost breakdown + plan recommendation + optimization tips
  pi-token-usage --export   [file.json]     Export all exchanges to JSON
  pi-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}  {DIM}(apply to all modes){RESET}
  --period <period>      Time filter — all, hour, "5 hours", today, yesterday, "7 days", "30 days", "3 months", "6 months", year

  Default period: today. Partial match works ("7" → "Last 7 days").

{BOLD}DATA SOURCE{RESET}
  {WHITE}pi{RESET}    {DIM}~/.pi/agent/sessions/{RESET}    ✓ Tokens ✓ Text ✓ Speed ✓ Tool calls

  {DIM}Local models (Ollama, …) report exact tokens but no price, so they show $0.00.{RESET}

{BOLD}QUICK START{RESET}
  pi-token-usage                              # Full overview, today
  pi-token-usage --period all                 # All time

{BOLD}PER-EXCHANGE DETAIL{RESET}
  pi-token-usage --prompts                    # Today's exchanges
  pi-token-usage -p --period "7 days"         # Last 7 days

{BOLD}EXPORT{RESET}
  pi-token-usage --export                     # Save to conversations.json
  pi-token-usage --export out.json --period "7 days"
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
        print(f"  Run {BOLD}pi-token-usage --help{RESET} for usage.\n")
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
