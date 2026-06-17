#!/usr/bin/env python3
"""
cursor-token-usage — Analyze Cursor agent usage from its local SQLite store.

Data source: ~/Library/Application Support/Cursor/User/globalStorage/state.vscdb
  composerData:<id>  + bubbleId:<id>:<bubble>  (+ workspaceStorage for projects)

Tokens are EXACT where Cursor recorded them; recent sessions zero out local
token counts (billing moved server-side), so those fall back to a text-length
estimate. Models are tagged [exact] or [est] accordingly.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
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

# ─── SQLite sources ────────────────────────────────────────────────────────
# Modern Cursor (late 2025+) stores conversations in the globalStorage SQLite
# KV store, not in ~/.cursor/projects/*/agent-transcripts/ anymore:
#   composerData:<composerId>          -> modelConfig.modelName, createdAt
#   bubbleId:<composerId>:<bubbleId>   -> type (1=user, 2=assistant),
#                                         tokenCount {inputTokens, outputTokens},
#                                         text, createdAt (ISO)
# Project attribution comes from each workspace's storage:
#   workspaceStorage/<hash>/workspace.json          -> folder (file:// URI)
#   workspaceStorage/<hash>/state.vscdb ItemTable
#       composer.composerData -> allComposers[].composerId
#
# Token counts are EXACT where Cursor recorded them (older sessions). Recent
# sessions zero out tokenCount locally (billing moved server-side), so we fall
# back to a text-length estimate and tag the model accordingly.

_GLOBAL_DB = (Path.home() / "Library" / "Application Support" / "Cursor"
              / "User" / "globalStorage" / "state.vscdb")
_WORKSPACE_BASE = (Path.home() / "Library" / "Application Support" / "Cursor"
                   / "User" / "workspaceStorage")

_CURSOR_DEFAULT_MODEL = "gpt-4o"   # for pricing fallback on Cursor-only names


def _query(db: Path, sql: str) -> list[tuple]:
    if not db.exists():
        return []
    try:
        import sqlite3
        # read-only, tolerate the app holding a write lock
        conn = sqlite3.connect(f"file:{db}?mode=ro&immutable=1", uri=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()
    except Exception:
        return []


def _composer_project_map() -> dict[str, str]:
    """Return {composerId: project_path} by joining each workspace's folder
    with the composer IDs that workspace owns."""
    import urllib.parse
    out: dict[str, str] = {}
    if not _WORKSPACE_BASE.exists():
        return out
    for ws_dir in _WORKSPACE_BASE.iterdir():
        if not ws_dir.is_dir():
            continue
        wj = ws_dir / "workspace.json"
        folder = None
        if wj.exists():
            try:
                uri = json.loads(wj.read_text()).get("folder", "")
                if uri.startswith("file://"):
                    folder = urllib.parse.unquote(uri[len("file://"):])
            except Exception:
                folder = None
        if not folder:
            continue
        rows = _query(ws_dir / "state.vscdb",
                      "SELECT value FROM ItemTable WHERE key='composer.composerData'")
        for (val,) in rows:
            try:
                data = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                continue
            for c in data.get("allComposers", []) or []:
                cid = c.get("composerId")
                if cid:
                    out[cid] = folder
    return out


def _composer_meta() -> dict[str, dict]:
    """Return {composerId: {model, created_dt}} from globalStorage composerData."""
    out: dict[str, dict] = {}
    for (val,) in _query(_GLOBAL_DB,
                         "SELECT value FROM cursorDiskKV WHERE key LIKE 'composerData:%'"):
        try:
            d = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            continue
        cid = d.get("composerId")
        if not cid:
            continue
        mc = d.get("modelConfig") or {}
        created = d.get("createdAt")
        created_dt = None
        if isinstance(created, (int, float)):
            try:
                created_dt = datetime.fromtimestamp(created / 1000, tz=timezone.utc)
            except (OSError, ValueError):
                created_dt = None
        out[cid] = {
            "model": mc.get("modelName") or mc.get("model") or "",
            "created": created_dt,
        }
    return out


def _parse_iso(s) -> datetime | None:
    if not isinstance(s, str) or not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def _price_model(raw_model: str) -> str:
    """Map a Cursor model name to one LiteLLM can price; fall back to gpt-4o."""
    from tokstat._core import match_model, ZERO_PRICE
    if raw_model and match_model(raw_model) != ZERO_PRICE:
        return raw_model
    return _CURSOR_DEFAULT_MODEL


# ─── Scanners ────────────────────────────────────────────────────────────────

def scan_cursor() -> list[dict]:
    """One record per assistant turn, from Cursor's SQLite KV store."""
    records = []
    for ex in _extract_exchanges_cursor():
        records.append({
            "tool":        "Cursor",
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


def _extract_exchanges_cursor() -> list[dict]:
    """Build exchanges from globalStorage bubbles, grouped per composer.

    A user bubble (type 1) opens an exchange; subsequent assistant bubbles
    (type 2) accumulate into it. Tokens are exact when Cursor stored them,
    estimated from text length otherwise (model tagged [exact] / [est]).
    """
    rows = _query(_GLOBAL_DB,
                  "SELECT key, value FROM cursorDiskKV WHERE key LIKE 'bubbleId:%'")
    if not rows:
        return []

    proj_map = _composer_project_map()
    meta = _composer_meta()

    # Group bubbles by composer, keeping (createdAt, bubble) for ordering.
    by_composer: dict[str, list] = defaultdict(list)
    for key, val in rows:
        parts = key.split(":")
        if len(parts) < 3:
            continue
        composer_id = parts[1]
        try:
            b = json.loads(val)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(b, dict):
            continue
        by_composer[composer_id].append(b)

    exchanges: list[dict] = []
    for composer_id, bubbles in by_composer.items():
        bubbles.sort(key=lambda b: b.get("createdAt") or "")
        cmeta = meta.get(composer_id, {})
        raw_model = cmeta.get("model") or _CURSOR_DEFAULT_MODEL
        project = proj_map.get(composer_id, "Cursor (unknown project)")

        current = None
        for b in bubbles:
            btype = b.get("type")
            ts = _parse_iso(b.get("createdAt")) or cmeta.get("created")
            text = (b.get("text") or "").strip()
            tc = b.get("tokenCount") or {}
            exact_in = int(tc.get("inputTokens", 0) or 0) if isinstance(tc, dict) else 0
            exact_out = int(tc.get("outputTokens", 0) or 0) if isinstance(tc, dict) else 0

            if btype == 1:  # user
                if current is not None:
                    exchanges.append(current)
                current = {
                    "tool":            "Cursor",
                    "project":         project,
                    "ts":              ts,
                    "user_text":       text[:500],
                    "assistant_texts": [],
                    "tool_errors":     [],
                    "tools_used":      defaultdict(int),
                    "num_turns":       0,
                    "tokens":          {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                    "cost":            0.0,
                    "_exact":          False,
                    "_raw_model":      raw_model,
                }
            elif btype == 2 and current is not None:  # assistant
                current["num_turns"] += 1
                if text:
                    current["assistant_texts"].append(text[:500])
                # Only count tokens Cursor actually recorded. Recent sessions
                # zero them out (billing is server-side) — we do NOT estimate,
                # since text-length guesses proved badly misleading. Such
                # exchanges still count as activity (prompts/turns).
                if exact_in or exact_out:
                    current["tokens"]["input"] += exact_in
                    current["tokens"]["output"] += exact_out
                    current["_exact"] = True
        if current is not None:
            exchanges.append(current)

    # Finalize: tag model and cost. Exact sessions get real cost; sessions
    # with no local token data are tagged [no tokens] and cost stays $0.
    for ex in exchanges:
        exact = ex.pop("_exact", False)
        raw_model = ex.pop("_raw_model", _CURSOR_DEFAULT_MODEL)
        if exact:
            ex["model"] = f"{raw_model} [exact]"
            ex["cost"] = compute_cost(ex["tokens"], _price_model(raw_model))
        else:
            ex["model"] = f"{raw_model} [no tokens]"
            ex["cost"] = 0.0

    # Keep every exchange that has at least a prompt or a turn — even those
    # with no token data, so activity (prompts/turns) is still reported.
    exchanges = [e for e in exchanges
                 if e.get("user_text") or e["num_turns"] > 0]
    return exchanges


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
    print(f"{DIM}  Note: tokens [exact] where Cursor recorded them; recent sessions [no tokens]{RESET}")
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

    if not _GLOBAL_DB.exists():
        print(f"  {DIM}Cursor not found at {_GLOBAL_DB}{RESET}\n")
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

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, [], cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges)
    print(f"  {DIM}⚠ [exact] = real token counts recorded by Cursor. [no tokens] = "
          f"recent sessions (billing tracked server-side, not stored locally) — "
          f"counted as activity only, tokens/cost shown as 0.{RESET}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {
    "cursor": "Cursor",
}

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
    for alias, name in _TOOL_ALIASES.items():
        if raw in alias or raw in name.lower():
            return name
    valid = ", ".join(sorted(set(_TOOL_ALIASES.values())))
    raise ValueError(f"Unknown tool '{args[idx + 1]}'. Available: {valid}")


def show_help():
    print(f"""
{BOLD}cursor-token-usage{RESET} — Analyze Cursor agent session activity.

{BOLD}NOTE{RESET}  {DIM}Reads Cursor's local SQLite store. Older sessions carry exact token
      counts ([exact]). Recent sessions track billing server-side and store
      no local token data ([no tokens]) — they're counted as activity
      (prompts/turns) but tokens/cost are not estimated.
      For authoritative totals: cursor.com/settings/usage.{RESET}

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
  {BLUE}Cursor{RESET}    {DIM}~/Library/Application Support/Cursor/User/globalStorage/state.vscdb{RESET}
            {DIM}tokens exact where recorded ([exact]), else [no tokens] (not estimated){RESET}
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
