#!/usr/bin/env python3
"""
kiro-token-usage — Aggregate and display token consumption from Kiro.

Data sources:
  - Tokens:    ~/Library/Application Support/Kiro/.../dev_data/devdata.sqlite
  - Project:   ~/Library/Application Support/Kiro/.../workspace-sessions/
  - Exchanges: ~/Library/Application Support/Kiro/.../{hash}/*.chat

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
    export_conversations, _parse_period, print_update_notice,
)

TOOL_COLORS["Kiro"] = YELLOW

_KIRO_BASE = (Path.home() / "Library" / "Application Support" / "Kiro"
              / "User" / "globalStorage" / "kiro.kiroagent")
_DB_PATH = _KIRO_BASE / "dev_data" / "devdata.sqlite"

# Kiro uses Anthropic models; default for pricing when model is "agent"/"auto"
_KIRO_DEFAULT_MODEL = "claude-sonnet-4-5"


# ─── Project resolution ───────────────────────────────────────────────────────

def _load_project_map() -> dict[str, str]:
    """Return {sessionId: workspaceDirectory} from all workspace-sessions."""
    result: dict[str, str] = {}
    sessions_dir = _KIRO_BASE / "workspace-sessions"
    if not sessions_dir.exists():
        return result
    for ws_dir in sessions_dir.iterdir():
        if not ws_dir.is_dir():
            continue
        sj = ws_dir / "sessions.json"
        if not sj.exists():
            continue
        try:
            for s in json.loads(open(sj).read()):
                sid = s.get("sessionId")
                wd = s.get("workspaceDirectory", "")
                if sid and wd:
                    result[sid] = wd
        except Exception:
            pass
    return result


def _most_recent_project() -> str:
    """Return the most recently active workspace directory."""
    sessions_dir = _KIRO_BASE / "workspace-sessions"
    if not sessions_dir.exists():
        return "unknown"
    best_ts = 0
    best_path = "unknown"
    for ws_dir in sessions_dir.iterdir():
        sj = ws_dir / "sessions.json"
        if not sj.exists():
            continue
        try:
            sessions = json.loads(open(sj).read())
            for s in sessions:
                ts = int(s.get("dateCreated", 0))
                wd = s.get("workspaceDirectory", "")
                if ts > best_ts and wd:
                    best_ts = ts
                    best_path = wd
        except Exception:
            pass
    return best_path


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_kiro() -> list[dict]:
    """Scan Kiro .chat files for token usage.

    Each exchange (deduplicated by user text) becomes one record.
    Input and output are estimated from conversation text length.
    """
    exchanges = _extract_exchanges_kiro()
    records = []
    for ex in exchanges:
        if ex["ts"] is None:
            continue
        records.append({
            "tool":        "Kiro",
            "model":       ex.get("model", _KIRO_DEFAULT_MODEL),
            "project":     ex["project"],
            "ts":          ex["ts"],
            "input":       ex["tokens"]["input"],
            "output":      ex["tokens"]["output"],
            "cache_read":  0,
            "cache_write": 0,
            "cost":        ex["cost"],
        })
    return records


def _normalize_model(model: str, provider: str) -> str:
    """Map Kiro's internal model names to pricing-compatible names."""
    if model in ("agent", "auto", "") or not model:
        return _KIRO_DEFAULT_MODEL
    # Kiro uses names like "claude-sonnet-4.5" → map to "claude-sonnet-4-5"
    name = model.replace(".", "-")
    return name


def _extract_exchanges_kiro() -> list[dict]:
    """Extract exchanges from Kiro .chat files.

    Each .chat file is one agent action. We deduplicate by workflowId,
    keeping the most complete version (most messages).
    """
    if not _KIRO_BASE.exists():
        return []

    project_map = _load_project_map()

    # Collect all .chat files, deduplicate by user_text prefix.
    # Kiro creates one .chat file per retry/step — all share the same user
    # prompt. Keep the version with the most assistant text (most complete).
    seen: dict[str, dict] = {}  # user_text[:200] -> best exchange

    for hash_dir in _KIRO_BASE.iterdir():
        if not (hash_dir.is_dir() and len(hash_dir.name) == 32):
            continue
        for chat_file in hash_dir.rglob("*.chat"):
            try:
                data = json.loads(open(chat_file, errors="replace").read())
            except (json.JSONDecodeError, OSError):
                continue

            meta = data.get("metadata") or {}
            chat = data.get("chat", [])
            if not chat:
                continue

            workflow_id = meta.get("workflowId", str(chat_file))
            start_ms = meta.get("startTime")
            ts = (datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc)
                  if start_ms else None)

            model_id = meta.get("modelId", "")
            model_name = _normalize_model(model_id, meta.get("modelProvider", ""))

            # Extract user text (first non-system human message)
            user_text = ""
            assistant_texts = []
            tools_used: dict[str, int] = defaultdict(int)

            for msg in chat:
                role = msg.get("role", "")
                content = msg.get("content", "")
                if isinstance(content, list):
                    text = " ".join(c.get("text", "") for c in content
                                    if isinstance(c, dict) and c.get("type") == "text")
                else:
                    text = str(content)

                if role == "human" and not user_text:
                    # Skip system injection (identity/instructions)
                    if not text.strip().startswith("<identity>"):
                        user_text = text.strip()[:500]
                elif role == "bot" and text.strip():
                    assistant_texts.append(text.strip())
                    # Detect tool calls in bot content
                    if isinstance(content, list):
                        for c in content:
                            if isinstance(c, dict) and c.get("type") not in ("text", None):
                                tools_used[c.get("type", "tool")] += 1

            if not user_text and not assistant_texts:
                continue

            # Estimate tokens from text length
            all_text_len = sum(len(m.get("content", "")) for m in chat
                               if isinstance(m.get("content"), str))
            out_text_len = sum(len(t) for t in assistant_texts)
            inp_est = max(all_text_len - out_text_len, 0) // 4
            out_est = out_text_len // 4

            tokens = {"input": inp_est, "output": out_est, "cache_read": 0, "cache_write": 0}
            cost = compute_cost(tokens, model_name)

            exchange = {
                "user_text":       user_text,
                "assistant_texts": assistant_texts,
                "tool_errors":     [],
                "tools_used":      dict(tools_used),
                "num_turns":       len([m for m in chat if m.get("role") == "bot"]),
                "model":           model_name,
                "project":         "unknown",
                "ts":              ts,
                "tokens":          tokens,
                "cost":            cost,
            }

            key = user_text[:200]
            prev = seen.get(key)
            prev_len = sum(len(t) for t in (prev or {}).get("assistant_texts", []))
            cur_len  = sum(len(t) for t in assistant_texts)
            if not prev or cur_len > prev_len:
                seen[key] = exchange

    exchanges = list(seen.values())

    # Resolve projects: try to match via timestamp to workspace sessions
    _assign_projects(exchanges, project_map)

    return exchanges


def _assign_projects(exchanges: list[dict], project_map: dict[str, str]) -> None:
    """Assign project paths to exchanges using workspace-sessions timestamps."""
    # Build sorted list of (dateCreated_ms, workspaceDirectory)
    sessions_dir = _KIRO_BASE / "workspace-sessions"
    timeline: list[tuple[int, str]] = []
    if sessions_dir.exists():
        for ws_dir in sessions_dir.iterdir():
            sj = ws_dir / "sessions.json"
            if not sj.exists():
                continue
            try:
                for s in json.loads(open(sj).read()):
                    ts = int(s.get("dateCreated", 0))
                    wd = s.get("workspaceDirectory", "")
                    if ts and wd:
                        timeline.append((ts, wd))
            except Exception:
                pass
    timeline.sort()

    for ex in exchanges:
        if ex["ts"] is None:
            ex["project"] = _most_recent_project()
            continue
        ex_ms = int(ex["ts"].timestamp() * 1000)
        # Find the active session at this timestamp
        project = timeline[0][1] if timeline else "unknown"
        for ts_ms, wd in timeline:
            if ts_ms <= ex_ms:
                project = wd
            else:
                break
        ex["project"] = project


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

    if not _DB_PATH.exists():
        print(f"  {DIM}Kiro not found at {_KIRO_BASE}{RESET}\n")
        return

    records = scan_kiro()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {YELLOW}●{RESET} {'Kiro':<12} {len(records):>6} records from {_KIRO_BASE.parent.parent}")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    show_overview_tables(records, [], cutoff, cutoff_end, period_label, tool_filter)


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"kiro": "Kiro"}

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
{BOLD}kiro-token-usage{RESET} — Aggregate and analyze Kiro token consumption.

{BOLD}MODES{RESET}
  kiro-token-usage                            Aggregated overview (period, project, model)
  kiro-token-usage --prompts  [-p]            Per-exchange detail (text, turns, tokens, cost)
  kiro-token-usage --anomalies                Technical anomaly detection
  kiro-token-usage --plan                     Cost breakdown + optimization tips
  kiro-token-usage --export   [file.json]     Export all exchanges to JSON
  kiro-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}
  --period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days", year

{BOLD}DATA SOURCES{RESET}
  {YELLOW}Kiro{RESET}
    Tokens:    {DIM}{_DB_PATH}{RESET}
    Exchanges: {DIM}{_KIRO_BASE}/{{hash}}/*.chat{RESET}
    Projects:  {DIM}{_KIRO_BASE}/workspace-sessions/{RESET}
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
