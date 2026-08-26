#!/usr/bin/env python3
"""
kiro-token-usage — Aggregate and display activity from Kiro.

Data source: per-session JSON under
  ~/Library/Application Support/Kiro/.../kiro.kiroagent/workspace-sessions/
      <base64(project path)>/<sessionId>.json   (history)
      <base64(project path)>/sessions.json       (dateCreated, project)

Kiro records no usable token counts (its tokens_generated log is always
zero with no per-message data), so tokstat reports activity only —
prompts/turns — with token/cost left at 0 ([no tokens]). It does NOT
estimate, to avoid misleading figures.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import sqlite3
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
    show_activity, show_total, show_impact, show_audit,
    export_conversations, _parse_period, _parse_region, print_update_notice,
    print_retention_alerts,
)

TOOL_COLORS["Kiro"] = YELLOW

_KIRO_BASE = (Path.home() / "Library" / "Application Support" / "Kiro"
              / "User" / "globalStorage" / "kiro.kiroagent")
_SESSIONS_DIR = _KIRO_BASE / "workspace-sessions"


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_kiro() -> list[dict]:
    """One record per assistant turn. Kiro does not store real token counts
    (its tokens_generated log is always zero), so token/cost are left at 0
    rather than estimated — see _extract_exchanges_kiro."""
    records = []
    for ex in _extract_exchanges_kiro():
        if ex["ts"] is None:
            continue
        records.append({
            "tool":        "Kiro",
            "model":       ex["model"],
            "project":     ex["project"],
            "ts":          ex["ts"],
            "input":       ex["tokens"]["input"],
            "output":      ex["tokens"]["output"],
            "cache_read":  0,
            "cache_write": 0,
            "cost":        ex["cost"],
        })
    return records


def _session_text(content) -> str:
    """Flatten a Kiro history message content (str or list of parts)."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        return " ".join(c.get("text", "") for c in content
                         if isinstance(c, dict) and c.get("type") == "text").strip()
    return ""


def _extract_exchanges_kiro() -> list[dict]:
    """Extract exchanges from Kiro's per-session JSON files.

    Modern Kiro stores each conversation at
      workspace-sessions/<base64(path)>/<sessionId>.json
    with a `history` array, `workspaceDirectory`, and a model title. The old
    {hash}/*.chat layout is gone. Kiro records no usable token counts
    (tokens_generated.jsonl has generatedTokens=0 and no per-message data),
    so we do NOT estimate — exchanges are counted as activity (prompts/turns)
    with token/cost left at 0, tagged [no tokens].
    """
    sessions_dir = _KIRO_BASE / "workspace-sessions"
    if not sessions_dir.exists():
        return []

    exchanges: list[dict] = []
    for ws_dir in sessions_dir.iterdir():
        if not ws_dir.is_dir():
            continue
        # Map sessionId -> (dateCreated_ms, workspaceDirectory, title)
        meta: dict[str, tuple] = {}
        sj = ws_dir / "sessions.json"
        if sj.exists():
            try:
                for s in json.loads(sj.read_text(errors="replace")):
                    sid = s.get("sessionId")
                    if sid:
                        meta[sid] = (int(s.get("dateCreated", 0) or 0),
                                     s.get("workspaceDirectory", "") or "unknown",
                                     s.get("title", ""))
            except (json.JSONDecodeError, OSError, ValueError):
                pass

        for sess_file in ws_dir.glob("*.json"):
            if sess_file.name == "sessions.json":
                continue
            try:
                data = json.loads(sess_file.read_text(errors="replace"))
            except (json.JSONDecodeError, OSError):
                continue
            sid = data.get("sessionId") or sess_file.stem
            ms, wd, _title = meta.get(sid, (0, "", ""))
            project = data.get("workspaceDirectory") or wd or "unknown"
            ts = (datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms
                  else datetime.fromtimestamp(sess_file.stat().st_mtime, tz=timezone.utc))

            history = data.get("history") or []
            current = None
            for item in history:
                msg = item.get("message") or {}
                role = msg.get("role", "")
                text = _session_text(msg.get("content", ""))
                if role == "user":
                    if current:
                        exchanges.append(current)
                    current = {
                        "user_text":       text[:500],
                        "assistant_texts": [],
                        "tool_errors":     [],
                        "tools_used":      defaultdict(int),
                        "num_turns":       0,
                        "model":           "Kiro Agent [no tokens]",
                        "project":         project,
                        "ts":              ts,
                        "tokens":          {"input": 0, "output": 0,
                                            "cache_read": 0, "cache_write": 0},
                        "cost":            0.0,
                    }
                elif role == "assistant" and current is not None:
                    current["num_turns"] += 1
                    if text:
                        current["assistant_texts"].append(text[:500])
            if current:
                exchanges.append(current)

    return [e for e in exchanges if e.get("user_text") or e["num_turns"] > 0]


def _arg_value(args, flag, default=None):
    if flag in args:
        i = args.index(flag)
        if i + 1 < len(args) and not args[i + 1].startswith("-"):
            return args[i + 1]
    return default


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Collect Kiro exchanges filtered by time."""
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

    _add("Kiro", _extract_exchanges_kiro())
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — Kiro{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning Kiro data...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not (_KIRO_BASE / "workspace-sessions").exists():
        print(f"  {DIM}Kiro not found at {_KIRO_BASE}{RESET}\n")
        return

    records = scan_kiro()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {YELLOW}●{RESET} {'Kiro':<12} {len(records):>6} records from {_KIRO_BASE.parent.parent}")
    print()
    print_retention_alerts(["Kiro"])
    print(f"  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, [], cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"kiro": "Kiro"}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--activity", "--total", "--impact", "--audit", "--judge", "--model", "--judge-max", "--verify", "--claude-judge", "--claude-model", "--export", "--period", "--since", "--tool",
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
{BOLD}kiro-token-usage{RESET} — Aggregate and analyze Kiro token consumption.

{BOLD}MODES{RESET}
  kiro-token-usage                            Aggregated overview (period, project, model)
  kiro-token-usage --prompts  [-p]            Per-exchange detail (text, turns, tokens, cost)
  kiro-token-usage --anomalies                Technical anomaly detection
  kiro-token-usage --activity                 Activity calendar (GitHub-style, by day)
  kiro-token-usage --total                    Compact totals (tokens + cost + data span)
  kiro-token-usage --impact                   Energy & CO₂ estimate (EcoLogits)
  kiro-token-usage --plan                     Cost breakdown + optimization tips
  kiro-token-usage --export   [file.json]     Export all exchanges to JSON
  kiro-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}
  --period <period>    all, today, yesterday, year, or any "N unit" (e.g. "5 days", "31 days", "2 weeks", "3 months"); default: today

{BOLD}DATA SOURCE{RESET}
  {YELLOW}Kiro{RESET}    {DIM}{_SESSIONS_DIR}/<project>/<sessionId>.json{RESET}
          {DIM}Kiro stores no token counts → activity only ([no tokens]){RESET}
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
        print(f"  Run {BOLD}kiro-token-usage --help{RESET} for usage.\n")
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
    elif "--activity" in args:
        show_activity(_collect_all_exchanges, period, tool)
    elif "--total" in args:
        show_total(_collect_all_exchanges, period, tool)
    elif "--impact" in args:
        show_impact(_collect_all_exchanges, period, tool, _parse_region(args))
    elif "--audit" in args:
        jmax_raw = _arg_value(args, "--judge-max")
        try:
            jmax = int(jmax_raw) if jmax_raw is not None else None
        except ValueError:
            jmax = None
        show_audit(_collect_all_exchanges, period, tool,
                   judge_model=_arg_value(args, "--model"), judge_max=jmax,
                   verify="--verify" in args,
                   claude_judge="--claude-judge" in args,
                   claude_model=_arg_value(args, "--claude-model"))
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
