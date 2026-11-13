#!/usr/bin/env python3
"""
qwen-token-usage — Aggregate and display token consumption from Qwen Code.

Scans ~/.qwen/projects/<encoded-cwd>/chats/<session>.jsonl transcripts (Qwen
Code, Alibaba's coding agent — a Gemini-CLI fork, `@qwen-code/qwen-code`).

Each transcript carries the working directory, model, per-turn token usage
(Gemini-API `usageMetadata`), message text and tool calls, so Qwen Code
supports the full feature set (overview, --prompts, --tool-use, --impact,
--plan, …). Local models (e.g. Ollama) report exact tokens but no price, so
they correctly show $0.00.

Token note (Gemini-API shape): promptTokenCount *includes*
cachedContentTokenCount, and candidatesTokenCount *includes* thoughtsTokenCount
(totalTokenCount = prompt + candidates). So fresh input = prompt − cached, and
output = candidates (reasoning already folded in).

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
    BOLD, DIM, RESET, YELLOW, RED,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact, show_tool_use, tool_target,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    project_matches,
)

# Register Qwen Code color (a distinct purple; basic palette is all taken)
TOOL_COLORS["Qwen Code"] = "\033[38;5;135m"

QWEN_PROJECTS = Path.home() / ".qwen" / "projects"
QWEN_USAGE = Path.home() / ".qwen" / "usage"
QWEN_USAGE_RECORD = Path.home() / ".qwen" / "usage_record.jsonl"


# ─── Shared helpers ────────────────────────────────────────────────────────────

def _ts(rec: dict):
    """Parse a record's ISO-8601 `timestamp`."""
    s = rec.get("timestamp")
    if not s:
        return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _usage_tokens(um: dict) -> dict:
    """Normalize Qwen's Gemini-API usageMetadata to tokstat's token dict.

    promptTokenCount includes cachedContentTokenCount → subtract so cached
    tokens aren't billed at the full input rate. candidatesTokenCount already
    includes thoughtsTokenCount → use it as-is for output.
    """
    cached = um.get("cachedContentTokenCount", 0) or 0
    prompt = um.get("promptTokenCount", 0) or 0
    return {
        "input":       max(prompt - cached, 0),
        "output":      um.get("candidatesTokenCount", 0) or 0,
        "cache_read":  cached,
        "cache_write": 0,
    }


def _part_text(parts) -> str:
    """Join the prose text parts of a message, skipping thinking parts."""
    if not isinstance(parts, list):
        return ""
    out = []
    for p in parts:
        if isinstance(p, dict) and p.get("text") and not p.get("thought"):
            out.append(p["text"])
    return "\n".join(out).strip()


def _iter_lines(jsonl_file: Path):
    """Yield parsed JSON objects from a Qwen transcript, skipping bad lines."""
    with open(jsonl_file, "r", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _transcripts():
    """All Qwen Code transcript files: projects/<encoded>/chats/<session>.jsonl."""
    if not QWEN_PROJECTS.exists():
        return []
    return QWEN_PROJECTS.glob("*/chats/*.jsonl")


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_qwen() -> list[dict]:
    """Scan Qwen Code transcripts for token usage (one record per assistant
    turn that carries usageMetadata)."""
    records: list[dict] = []
    for jsonl_file in _transcripts():
        try:
            for rec in _iter_lines(jsonl_file):
                if rec.get("type") != "assistant":
                    continue
                um = rec.get("usageMetadata") or {}
                if not um:
                    continue
                tokens = _usage_tokens(um)
                if not any(tokens.values()):
                    continue
                ts = _ts(rec)
                if ts is None:
                    continue
                model = rec.get("model") or "qwen-unknown"
                records.append({
                    "tool":       "Qwen Code",
                    "model":      model,
                    "project":    rec.get("cwd") or "unknown",
                    "ts":         ts,
                    "session_id": rec.get("sessionId") or jsonl_file.stem,
                    **tokens,
                    "cost":       compute_cost(tokens, model),
                })
        except (OSError, IOError):
            continue
    return records


def _session_project_map() -> dict[str, str]:
    """Map sessionId → working directory, from the per-session usage ledger
    (clean absolute paths), so speed records (built from the token ledger, which
    has no cwd) can still be filtered by project."""
    mapping: dict[str, str] = {}
    try:
        if QWEN_USAGE_RECORD.exists():
            for rec in _iter_lines(QWEN_USAGE_RECORD):
                sid, proj = rec.get("sessionId"), rec.get("project")
                if sid and proj:
                    mapping[sid] = proj
    except (OSError, IOError):
        pass
    return mapping


def scan_speed_qwen() -> list[dict]:
    """Output speed (tokens/sec) from Qwen Code's token-usage ledger.

    The ledger (~/.qwen/usage/token-usage-*.jsonl) records an exact
    `apiDurationMs` per request, so the rate is precise — no timestamp
    heuristic. The ledger carries no cwd, so project comes from the
    session→project map."""
    results: list[dict] = []
    if not QWEN_USAGE.exists():
        return results
    proj_map = _session_project_map()
    for ledger in QWEN_USAGE.glob("token-usage-*.jsonl"):
        try:
            for rec in _iter_lines(ledger):
                dur_ms = rec.get("apiDurationMs") or 0
                out = rec.get("outputTokens") or 0
                if dur_ms <= 0 or out < 10:
                    continue
                dt = dur_ms / 1000.0
                speed = out / dt
                if speed > 2000:          # guard against clock artifacts
                    continue
                ts = _ts(rec)
                if ts is None:
                    continue
                sid = rec.get("sessionId")
                results.append({
                    "tool":       "Qwen Code",
                    "model":      rec.get("model") or "qwen-unknown",
                    "project":    proj_map.get(sid, "unknown"),
                    "ts":         ts,
                    "session_id": sid,
                    "tokens":     out,
                    "duration":   dt,
                    "speed":      speed,
                })
        except (OSError, IOError):
            continue
    return results


# ─── Exchanges ─────────────────────────────────────────────────────────────────

def _extract_exchanges_qwen(jsonl_path: str) -> list[dict]:
    """Parse one Qwen Code transcript into exchanges (one per user prompt)."""
    try:
        lines = list(_iter_lines(Path(jsonl_path)))
    except (OSError, IOError):
        return []

    exchanges: list[dict] = []
    current = None
    calls_by_id: dict = {}
    fallback_session = Path(jsonl_path).stem

    def _new(user_text, ts, cwd, session_id):
        return {
            "user_text": user_text, "assistant_texts": [], "tool_errors": [],
            "tools_used": {}, "num_turns": 0,
            "model": "qwen-unknown",
            "project": cwd, "ts": ts, "last_ts": ts,
            "first_assistant_ts": None,
            "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
            "cost": 0.0,
            "context_peak": 0, "context_first": None, "context_last": 0,
            "compactions": [],
            "tool_calls": [],
            "session_id": session_id or fallback_session,
        }

    for rec in lines:
        rtype = rec.get("type")
        ts = _ts(rec)
        cwd = rec.get("cwd")
        sid = rec.get("sessionId")
        msg = rec.get("message") or {}

        if rtype == "user":
            if current:
                exchanges.append(current)
            calls_by_id = {}
            current = _new(_part_text(msg.get("parts")), ts, cwd, sid)

        elif rtype == "assistant":
            if current is None:
                current = _new("", ts, cwd, sid)
                calls_by_id = {}
            if ts:
                current["last_ts"] = ts
                if current["first_assistant_ts"] is None:
                    current["first_assistant_ts"] = ts
            current["num_turns"] += 1
            if rec.get("model"):
                current["model"] = rec["model"]

            um = rec.get("usageMetadata") or {}
            if um:
                tk = _usage_tokens(um)
                for key in ("input", "output", "cache_read", "cache_write"):
                    current["tokens"][key] += tk[key]
                current["cost"] += compute_cost(tk, current["model"])
                ctx = (um.get("promptTokenCount", 0) or 0)
                if ctx > 0:
                    if ctx > current["context_peak"]:
                        current["context_peak"] = ctx
                    if current["context_first"] is None:
                        current["context_first"] = ctx
                    current["context_last"] = ctx

            for p in msg.get("parts", []) or []:
                if not isinstance(p, dict):
                    continue
                if p.get("text") and not p.get("thought"):
                    t = p["text"].strip()
                    if t:
                        current["assistant_texts"].append(t)
                elif p.get("functionCall"):
                    fc = p["functionCall"]
                    name = fc.get("name") or "?"
                    args = fc.get("args") if isinstance(fc.get("args"), dict) else {}
                    call = {"ts": ts, "name": name,
                            "target": tool_target(name, args), "error": False,
                            "id": fc.get("id")}
                    current["tool_calls"].append(call)
                    current["tools_used"][name] = current["tools_used"].get(name, 0) + 1
                    if fc.get("id"):
                        calls_by_id[fc["id"]] = call

        elif rtype == "tool_result":
            if current is None:
                continue
            if ts:
                current["last_ts"] = ts
            tcr = rec.get("toolCallResult") or {}
            is_error = (tcr.get("status") == "error"
                        or tcr.get("executionStatus") == "error")
            if is_error:
                call = calls_by_id.get(tcr.get("callId"))
                if call:
                    call["error"] = True
                name = call["name"] if call else (tcr.get("errorType") or "tool")
                current["tool_errors"].append(f"{name}: {tcr.get('errorType') or 'error'}")

        # rtype == "system" and anything else: ignored

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
    """Collect Qwen Code exchanges filtered by time."""
    all_exchanges: list[dict] = []
    tool_counts: dict[str, int] = {}

    if tool_filter and tool_filter != "Qwen Code":
        return all_exchanges, tool_counts

    for jsonl_file in _transcripts():
        exs = _extract_exchanges_qwen(str(jsonl_file))
        filtered = [ex for ex in exs
                    if ex["ts"] and ex["ts"] >= cutoff
                    and (cutoff_end is None or ex["ts"] < cutoff_end)]
        for ex in filtered:
            ex["tool"] = "Qwen Code"
        if filtered:
            all_exchanges.extend(filtered)
            tool_counts["Qwen Code"] = tool_counts.get("Qwen Code", 0) + len(filtered)

    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None,
         by_session: bool = False, by_project: bool = False,
         project_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — Qwen Code{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.qwen/projects/.../chats/...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not QWEN_PROJECTS.exists():
        print(f"  {DIM}Qwen Code not found at ~/.qwen/projects/{RESET}\n")
        return

    records = scan_qwen()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]
    if project_filter:
        records = [r for r in records if project_matches(r.get("project"), project_filter)]

    color = TOOL_COLORS["Qwen Code"]
    if records:
        print(f"  {color}●{RESET} {'Qwen Code':<12} {len(records):>6} records from ~/.qwen/")
    print()
    print(f"  Period: {BOLD}{period_label}{RESET}")
    if project_filter:
        print(f"  Project: {BOLD}{project_filter}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    speed_records = scan_speed_qwen()
    speed_records = [sr for sr in speed_records
                     if sr["ts"] >= cutoff and (cutoff_end is None or sr["ts"] < cutoff_end)]

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, speed_records, cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges,
                         by_session=by_session, by_project=by_project,
                         project_filter=project_filter)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"qwen": "Qwen Code", "qwen-code": "Qwen Code", "qwencode": "Qwen Code"}

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
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: qwen")


def show_help():
    print(f"""
{BOLD}qwen-token-usage{RESET} — Aggregate and analyze Qwen Code token consumption.

{BOLD}MODES{RESET}
  qwen-token-usage                            Aggregated overview (period, project, model, speed)
  qwen-token-usage --prompts  [-p]            Per-exchange detail (text, model, turns, tokens)
  qwen-token-usage --anomalies                Technical anomaly detection (cost, cache, tool storms)
  qwen-token-usage --activity                 Activity calendar (GitHub-style, by day)
  qwen-token-usage --total                    Compact totals (tokens + cost + data span)
  qwen-token-usage --impact                   Energy & CO₂ estimate (EcoLogits)
  qwen-token-usage --by-session               Overview + per-session table (all sessions)
  qwen-token-usage --by-project               Overview + a project → sessions drill-down
  qwen-token-usage --project <name>           Scope every view to one project (substring)
  qwen-token-usage --session <id>             Scope every view to one session (id or short handle)
  qwen-token-usage --tool-use                 Timeline of tool calls (file/command + time)
  qwen-token-usage --plan                     Cost breakdown + plan recommendation + optimization tips
  qwen-token-usage --export   [file.json]     Export all exchanges to JSON
  qwen-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}  {DIM}(apply to all modes){RESET}
  --period <period>      Time filter — all, hour, "5 hours", today, yesterday, "7 days", "30 days", "3 months", "6 months", year

  Default period: today. Partial match works ("7" → "Last 7 days").

{BOLD}DATA SOURCE{RESET}
  {TOOL_COLORS["Qwen Code"]}Qwen Code{RESET}    {DIM}~/.qwen/projects/.../chats/*.jsonl{RESET}    ✓ Tokens ✓ Text ✓ Tool calls

  {DIM}Local models (Ollama, …) report exact tokens but no price, so they show $0.00.{RESET}

{BOLD}QUICK START{RESET}
  qwen-token-usage                              # Full overview, today
  qwen-token-usage --period all                 # All time

{BOLD}EXPORT{RESET}
  qwen-token-usage --export                     # Save to conversations.json
  qwen-token-usage --export out.json --period "7 days"
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
        print(f"  Run {BOLD}qwen-token-usage --help{RESET} for usage.\n")
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
