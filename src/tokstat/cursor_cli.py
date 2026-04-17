#!/usr/bin/env python3
"""
cursor-token-usage — Analyze Cursor agent session activity from local transcripts.

Data source: ~/.cursor/projects/*/agent-transcripts/**/*.jsonl
Token counts are estimated (real counts tracked server-side by Cursor).
Estimates are 5-15x lower than reality — tool outputs not stored locally.

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
    normalize_project, _warm_worktree_cache,
    shorten_path, fmt_tokens, fmt_cost,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    export_conversations, _parse_period, print_update_notice,
)

TOOL_COLORS["Cursor"] = BLUE

_TRANSCRIPTS_BASE = Path.home() / ".cursor" / "projects"

# ─── SQLite helpers ──────────────────────────────────────────────────────────

_DB_PATH = (Path.home() / "Library" / "Application Support" / "Cursor"
            / "User" / "globalStorage" / "state.vscdb")


def _load_session_models() -> dict[str, str]:
    """Return {session_uuid: model_name} from Cursor's SQLite composerData."""
    models: dict[str, str] = {}
    if not _DB_PATH.exists():
        return models
    try:
        import sqlite3
        conn = sqlite3.connect(str(_DB_PATH))
        cur = conn.cursor()
        cur.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE 'composerData:%'")
        for key, val in cur.fetchall():
            try:
                d = json.loads(val)
                cid = d.get("composerId", "")
                mc = d.get("modelConfig") or {}
                m = mc.get("modelName") or mc.get("model") or ""
                if cid and m:
                    models[cid] = m
            except Exception:
                pass
        conn.close()
    except Exception:
        pass
    return models


# ─── Token estimation heuristics ─────────────────────────────────────────────
_CURSOR_SYSTEM_PROMPT_TOKENS = 3_000
_TOOL_OUTPUT_TOKENS: dict[str, int] = {
    "Shell":         3_000,
    "ReadFile":      5_000,
    "WebFetch":      7_000,
    "Glob":            300,
    "ApplyPatch":      500,
    "ReadLints":       300,
    "GenerateImage":   200,
    "Search":        1_000,
    "default":         800,
}
# Cursor default model for pricing (model=auto → gpt-4o equivalent)
_CURSOR_DEFAULT_MODEL = "gpt-4o"


# ─── Project path decoding ────────────────────────────────────────────────────

def _decode_project_path(dirname: str) -> str:
    """Decode Cursor's project directory name to a filesystem path."""
    candidate = "/" + dirname.replace("-", "/")
    if Path(candidate).exists():
        return candidate
    parts = dirname.split("-")
    current = Path("/")
    i = 0
    while i < len(parts):
        matched = False
        for j in range(len(parts), i, -1):
            for sep in (" ", "-"):
                name = sep.join(parts[i:j])
                if (current / name).exists():
                    current = current / name
                    i = j
                    matched = True
                    break
            if matched:
                break
        if not matched:
            return "/" + dirname.replace("-", "/")
    return str(current)


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_cursor() -> list[dict]:
    """Scan Cursor agent-transcript JSONL files for token usage records.

    Returns one record per exchange (user turn + assistant response).
    Token counts are estimated from context accumulation + tool heuristics.
    """
    exchanges = _parse_all_transcripts()
    records = []
    for ex in exchanges:
        records.append({
            "tool":        "Cursor",
            "model":       ex.get("model", _CURSOR_DEFAULT_MODEL + " [est]"),
            "project":     ex["project"],
            "ts":          ex["ts"],
            "input":       ex["tokens"]["input"],
            "output":      ex["tokens"]["output"],
            "cache_read":  0,
            "cache_write": 0,
            "cost":        ex["cost"],
        })
    return records


def _parse_all_transcripts() -> list[dict]:
    """Parse all Cursor agent-transcript JSONL files into exchange dicts."""
    if not _TRANSCRIPTS_BASE.exists():
        return []

    session_models = _load_session_models()
    exchanges = []

    for proj_dir in _TRANSCRIPTS_BASE.iterdir():
        if not proj_dir.is_dir():
            continue

        project_path = _decode_project_path(proj_dir.name)

        tools_dir = proj_dir / "agent-tools"
        webfetch_tokens = sum(
            f.stat().st_size // 4 for f in tools_dir.iterdir() if f.is_file()
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

            session_ts = datetime.fromtimestamp(jsonl.stat().st_mtime, tz=timezone.utc)
            session_model = session_models.get(session_dir.name, _CURSOR_DEFAULT_MODEL)

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

            current = None
            context_chars = 0
            webfetch_allocated = False

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
                        "model":           session_model + " [est]",
                        "project":         project_path,
                        "ts":              session_ts,
                        "user_text":       user_text,
                        "assistant_texts": [],
                        "tool_errors":     [],
                        "tools_used":      defaultdict(int),
                        "num_turns":       0,
                        "tokens":          {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                        "cost":            0.0,
                        "_ctx":            context_chars,
                        "_wf":             webfetch_tokens,
                        "_wf_used":        webfetch_allocated,
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
                                current["tools_used"][c.get("name", "unknown")] += 1
                    context_chars += msg_chars

            if current is not None:
                exchanges.append(current)

    # Compute input token estimates and costs
    for ex in exchanges:
        ctx = ex.pop("_ctx", 0) // 4
        wf_tokens = ex.pop("_wf", 0)
        wf_used = ex.pop("_wf_used", False)

        inp = _CURSOR_SYSTEM_PROMPT_TOKENS + ctx
        wf_count = 0
        for tool_name, count in ex["tools_used"].items():
            if tool_name == "WebFetch":
                wf_count += count
            else:
                inp += _TOOL_OUTPUT_TOKENS.get(tool_name, _TOOL_OUTPUT_TOKENS["default"]) * count
        if wf_count > 0:
            if wf_tokens > 0 and not wf_used:
                inp += wf_tokens
            else:
                inp += _TOOL_OUTPUT_TOKENS["WebFetch"] * wf_count

        ex["tokens"]["input"] = inp
        # For pricing: use real model name; fall back to default for Cursor-specific names
        raw_model = ex["model"].replace(" [est]", "")
        from tokstat._core import match_model, ZERO_PRICE
        if match_model(raw_model) == ZERO_PRICE:
            raw_model = _CURSOR_DEFAULT_MODEL
        ex["cost"] = compute_cost(ex["tokens"], raw_model)

    return exchanges


def _extract_exchanges_cursor() -> list[dict]:
    """Return Cursor exchanges in standard format for display modes."""
    return _parse_all_transcripts()


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    """Collect Cursor exchanges filtered by time."""
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

    _add("Cursor", _extract_exchanges_cursor())
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main (aggregated overview) ──────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — Cursor{RESET}")
    print(f"{DIM}  Note: token counts are estimated [est] — real counts tracked server-side{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning ~/.cursor/projects/...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not _TRANSCRIPTS_BASE.exists():
        print(f"  {DIM}Cursor not found at {_TRANSCRIPTS_BASE}{RESET}\n")
        return

    records = scan_cursor()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        est_count = len(records)
        print(f"  {BLUE}●{RESET} {'Cursor':<12} {est_count:>6} records [est] from ~/.cursor/")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    show_overview_tables(records, [], cutoff, cutoff_end, period_label, tool_filter)
    print(f"  {DIM}⚠ All token counts are estimates — Shell/ReadFile outputs not stored locally.{RESET}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {
    "cursor": "Cursor",
}

_KNOWN_FLAGS = {
    "--help", "-h", "--prompts", "-p", "--anomalies",
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
    for alias, name in _TOOL_ALIASES.items():
        if raw in alias or raw in name.lower():
            return name
    valid = ", ".join(sorted(set(_TOOL_ALIASES.values())))
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: {valid}")


def show_help():
    print(f"""
{BOLD}cursor-token-usage{RESET} — Analyze Cursor agent session activity.

{BOLD}NOTE{RESET}  {DIM}Cursor tracks token counts server-side. This tool shows estimated tokens
      based on conversation text + tool output heuristics. Estimates can be
      5-15x lower than reality (Shell/ReadFile outputs not stored locally).
      For exact counts: cursor.com/settings/usage → Export CSV.{RESET}

{BOLD}MODES{RESET}
  cursor-token-usage                            Aggregated overview (period, project, model)
  cursor-token-usage --prompts  [-p]            Per-exchange detail (text, turns, tools, cost)
  cursor-token-usage --anomalies                Technical anomaly detection
  cursor-token-usage --plan                     Cost breakdown + optimization tips
  cursor-token-usage --export   [file.json]     Export all exchanges to JSON
  cursor-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}
  --period <period>    all, hour, "5 hours", today, yesterday, "7 days", "30 days", year

{BOLD}DATA SOURCE{RESET}
  {BLUE}Cursor{RESET}    {DIM}~/.cursor/projects/*/agent-transcripts/{RESET}
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
