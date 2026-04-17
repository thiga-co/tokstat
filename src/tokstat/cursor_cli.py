#!/usr/bin/env python3
"""
cursor-token-usage — Estimate token consumption from Cursor.

Reads from Cursor's local SQLite database. Token counts use real data when
available; otherwise estimated from text length (~4 chars/token). Estimated
entries are marked [est]. Cursor tracks costs server-side so real counts are
only partially available locally.

SPDX-License-Identifier: MIT
Copyright (c) 2026 Olivier Bergeret
"""

from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from tokstat.cli import __version__
from tokstat._core import (
    BOLD, DIM, RESET, BLUE, YELLOW, RED,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    export_conversations, _parse_period, print_update_notice,
)

TOOL_COLORS["Cursor"] = BLUE

_DB_PATH = (Path.home() / "Library" / "Application Support" / "Cursor"
            / "User" / "globalStorage" / "state.vscdb")


# ─── Model name normalization ─────────────────────────────────────────────────

def _cursor_model_name(raw: str) -> str:
    """Normalize Cursor's internal model names to standard names for pricing."""
    if not raw or raw in ("default", "unknown", ""):
        return "cursor-default"
    m = raw.lower().replace("-high-thinking", "").replace("-thinking", "")
    match = re.match(r"claude[- ](\d+)\.(\d+)[- ](opus|sonnet|haiku)", m)
    if match:
        return f"claude-{match.group(3)}-{match.group(1)}-{match.group(2)}"
    match = re.match(r"gpt[- ](\d+)\.(\d+)[- ](.*)", m)
    if match:
        return f"gpt-{match.group(1)}.{match.group(2)}-{match.group(3)}"
    return raw


# ─── Scanner ─────────────────────────────────────────────────────────────────

def scan_cursor() -> list[dict]:
    """Scan Cursor state.vscdb for token usage.

    Uses real tokenCount when available; otherwise estimates from text length.
    Estimated entries have model name suffixed with [est].
    """
    records = []
    if not _DB_PATH.exists():
        return records
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        cur = conn.cursor()

        # Build composerId -> (project, model) lookup
        composer_projects: dict[str, str] = {}
        composer_models: dict[str, str] = {}
        cur.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                    ("composerData:%",))
        for key, val in cur.fetchall():
            try:
                cdata = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                continue
            cid = cdata.get("composerId", "")
            if not cid:
                continue
            all_dirs = []
            for src_key in ("originalFileStates", "newlyCreatedFiles", "newlyCreatedFolders"):
                src = cdata.get(src_key)
                items = list(src.keys()) if isinstance(src, dict) else (src or [])
                for item in items:
                    fp = item if isinstance(item, str) else (item.get("uri", {}).get("path", "")
                                                              if isinstance(item, dict) else "")
                    if fp.startswith("file://"):
                        fp = fp[7:]
                    if not fp:
                        continue
                    import os
                    all_dirs.append(fp if src_key == "newlyCreatedFolders"
                                    else str(Path(fp).parent))
            if all_dirs:
                try:
                    import os
                    composer_projects[cid] = os.path.commonpath(all_dirs)
                except ValueError:
                    pass
            mc = cdata.get("modelConfig") or {}
            composer_models[cid] = mc.get("modelName", "")

        # Process bubbles
        cur.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                    ("bubbleId:%",))
        for (key, val) in cur.fetchall():
            try:
                data = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                continue
            key_parts = key.split(":")
            composer_id = key_parts[1] if len(key_parts) >= 3 else ""
            if data.get("type", 0) != 2:  # only assistant messages
                continue

            ts_str = data.get("createdAt", "")
            if not ts_str:
                continue
            try:
                if isinstance(ts_str, (int, float)):
                    ts = datetime.fromtimestamp(ts_str / 1000, tz=timezone.utc)
                else:
                    ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, AttributeError, OSError):
                continue

            project = composer_projects.get(composer_id, "") or "unknown"
            comp_model = composer_models.get(composer_id, "")
            model_info = data.get("modelInfo") or {}
            model_raw = model_info.get("modelName", "") or comp_model
            model = _cursor_model_name(model_raw)

            tc = data.get("tokenCount") or {}
            inp = tc.get("inputTokens", 0)
            out = tc.get("outputTokens", 0)
            estimated = False

            if inp == 0 and out == 0:
                text_len = len(data.get("text", ""))
                thinking = data.get("thinking") or data.get("allThinkingBlocks") or []
                thinking_len = 0
                if isinstance(thinking, dict):
                    thinking_len = len(thinking.get("text", ""))
                elif isinstance(thinking, list):
                    for tb in thinking:
                        if isinstance(tb, dict):
                            thinking_len += len(tb.get("thinking", ""))
                out = (text_len + thinking_len) // 4
                if out == 0:
                    continue
                estimated = True

            tokens = {"input": inp, "output": out, "cache_read": 0, "cache_write": 0}
            model_label = f"{model} [est]" if estimated else model
            records.append({
                "tool":    "Cursor",
                "model":   model_label,
                "project": project,
                "ts":      ts,
                **tokens,
                "cost":    compute_cost(tokens, model),
            })

        conn.close()
    except (sqlite3.Error, OSError):
        pass
    return records


def _extract_exchanges_cursor() -> list[dict]:
    """Extract exchanges from Cursor SQLite database."""
    if not _DB_PATH.exists():
        return []

    exchanges = []
    try:
        conn = sqlite3.connect(str(_DB_PATH))
        cur = conn.cursor()

        composer_projects: dict[str, str] = {}
        composer_models: dict[str, str] = {}
        cur.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                    ("composerData:%",))
        for key, val in cur.fetchall():
            try:
                cdata = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                continue
            cid = cdata.get("composerId", "")
            if not cid:
                continue
            all_dirs = []
            for src_key in ("originalFileStates", "newlyCreatedFiles", "newlyCreatedFolders"):
                src = cdata.get(src_key)
                items = list(src.keys()) if isinstance(src, dict) else (src or [])
                for item in items:
                    fp = item if isinstance(item, str) else (item.get("uri", {}).get("path", "")
                                                              if isinstance(item, dict) else "")
                    if fp.startswith("file://"):
                        fp = fp[7:]
                    if not fp:
                        continue
                    all_dirs.append(fp if src_key == "newlyCreatedFolders"
                                    else str(Path(fp).parent))
            if all_dirs:
                try:
                    import os
                    composer_projects[cid] = os.path.commonpath(all_dirs)
                except ValueError:
                    pass
            mc = cdata.get("modelConfig") or {}
            composer_models[cid] = mc.get("modelName", "") or "cursor-default"

        composer_bubbles: dict[str, list] = {}
        cur.execute("SELECT key, value FROM cursorDiskKV WHERE key LIKE ?",
                    ("bubbleId:%",))
        for (key, val) in cur.fetchall():
            try:
                data = json.loads(val)
            except (json.JSONDecodeError, TypeError):
                continue
            key_parts = key.split(":")
            cid = key_parts[1] if len(key_parts) >= 3 else ""
            composer_bubbles.setdefault(cid, []).append(data)

        for composer_id, bubbles in composer_bubbles.items():
            project = composer_projects.get(composer_id, "") or "unknown"
            comp_model = _cursor_model_name(composer_models.get(composer_id, "") or "cursor-default")
            sorted_bubbles = sorted(bubbles, key=lambda b: b.get("createdAt", 0))

            current = None
            for data in sorted_bubbles:
                btype = data.get("type", 0)
                text = data.get("text", "").strip()
                ts_str = data.get("createdAt", "")
                ts = None
                if ts_str:
                    try:
                        if isinstance(ts_str, (int, float)):
                            ts = datetime.fromtimestamp(ts_str / 1000, tz=timezone.utc)
                        else:
                            ts = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
                    except (ValueError, AttributeError, OSError):
                        pass

                if btype == 1:  # user message
                    if current:
                        exchanges.append(current)
                    current = {
                        "user_text": text, "assistant_texts": [], "tool_errors": [],
                        "tools_used": defaultdict(int), "num_turns": 0,
                        "model": comp_model, "project": project, "ts": ts,
                        "tokens": {"input": 0, "output": 0, "cache_read": 0, "cache_write": 0},
                        "cost": 0.0,
                    }

                elif btype == 2 and current:  # assistant message
                    current["num_turns"] += 1
                    if text:
                        current["assistant_texts"].append(text)
                    tc = data.get("tokenCount") or {}
                    inp = tc.get("inputTokens", 0)
                    out = tc.get("outputTokens", 0)
                    if inp == 0 and out == 0:
                        out = len(text) // 4
                    if inp > 0 or out > 0:
                        current["tokens"]["input"]  += inp
                        current["tokens"]["output"] += out
                        current["cost"] += compute_cost(
                            {"input": inp, "output": out, "cache_read": 0, "cache_write": 0},
                            comp_model,
                        )

            if current:
                exchanges.append(current)

        conn.close()
    except (sqlite3.Error, OSError):
        pass
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
    print(f"{DIM}  Note: token counts are estimated when not stored locally [est]{RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")
    print(f"{DIM}  Scanning Cursor database...{RESET}\n")

    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    if not _DB_PATH.exists():
        print(f"  {DIM}Cursor not found at {_DB_PATH}{RESET}\n")
        return

    records = scan_cursor()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        est = sum(1 for r in records if "[est]" in r["model"])
        note = f" ({est} estimated)" if est else ""
        print(f"  {BLUE}●{RESET} {'Cursor':<12} {len(records):>6} records{note}")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No token usage data found.{RESET}\n")
        return

    show_overview_tables(records, [], cutoff, cutoff_end, period_label, tool_filter)


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
{BOLD}cursor-token-usage{RESET} — Estimate Cursor token consumption from local data.

{BOLD}NOTE{RESET}  {DIM}Cursor tracks costs server-side. Local token counts are real when
      available, estimated from text length (~4 chars/token) otherwise.
      Estimated entries are marked [est] in the model column.{RESET}

{BOLD}MODES{RESET}
  cursor-token-usage                            Aggregated overview (period, project, model)
  cursor-token-usage --prompts  [-p]            Per-exchange detail
  cursor-token-usage --anomalies                Technical anomaly detection
  cursor-token-usage --plan                     Cost breakdown + optimization tips
  cursor-token-usage --export   [file.json]     Export all exchanges to JSON
  cursor-token-usage --help     [-h]            This help

{BOLD}FILTERS{RESET}
  --period <period>      all, hour, "5 hours", today, yesterday, "7 days", "30 days", year

{BOLD}DATA SOURCE{RESET}
  {BLUE}Cursor{RESET}    {DIM}~/Library/Application Support/Cursor/User/globalStorage/state.vscdb{RESET}
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
