#!/usr/bin/env python3
"""
claude-web-token-usage — Token usage from a claude.ai data export.

This tool no longer scrapes claude.ai. Anthropic's private endpoints
rate-limit aggressively and the cookie-auth path was unreliable; use
the official data export (claude.ai → Settings → Privacy → Export
Data, you'll get an email with a ZIP) and feed it via `--import`.

Token counts are estimated from message text length (chars / 4); cost
is computed from LiteLLM pricing using each message's `model` field.

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
from zipfile import ZipFile

from tokstat.cli import __version__
from tokstat._core import (
    BOLD, DIM, RESET, YELLOW, RED, MAGENTA,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    show_activity, show_total, show_impact,
    export_conversations, _parse_period, _parse_region, print_update_notice,
)
from tokstat._web import (
    cache_iter, cache_save, imported_accounts,
    cache_orphans, clean_orphans, clear_imports,
)

TOOL_NAME = "Claude.ai"
TOOL_COLORS[TOOL_NAME] = MAGENTA

_SERVICE = "claude.ai"


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _project_label(account: str) -> str:
    return f"claude.ai ({account})"


def _cache_id(account: str, conv_id: str) -> str:
    return f"{account}__{conv_id}"


def _parse_dt(s) -> datetime | None:
    if not s:
        return None
    if isinstance(s, (int, float)):
        try:
            return datetime.fromtimestamp(float(s), tz=timezone.utc)
        except (OSError, ValueError):
            return None
    try:
        return datetime.fromisoformat(str(s).replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _message_text(msg: dict) -> str:
    parts = msg.get("content") or []
    chunks: list[str] = []
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict) and p.get("type") == "text":
                t = p.get("text", "")
                if t:
                    chunks.append(t)
    if not chunks and msg.get("text"):
        chunks.append(msg["text"])
    return "\n".join(chunks)


def _conv_messages(conv: dict) -> list[dict]:
    msgs = conv.get("chat_messages") or []
    out = []
    for m in msgs:
        ts = _parse_dt(m.get("created_at"))
        if ts is None:
            continue
        out.append({
            "ts":     ts,
            "sender": m.get("sender", ""),
            "text":   _message_text(m),
            "model":  m.get("model") or conv.get("model") or "claude-unknown",
        })
    out.sort(key=lambda m: m["ts"])
    return out


# ─── Import (official data export) ───────────────────────────────────────────

_EXPORT_PATTERN = re.compile(r"(^|/)conversations(?:-\d+)?\.json$")


def _import_from_path(path: str, account: str) -> int:
    """Populate the local cache from a claude.ai data export — accepts a
    directory, a ZIP, or one of the conversations*.json files directly."""
    p = Path(path).expanduser()
    if not p.exists():
        raise FileNotFoundError(f"no such file or directory: {p}")
    chunks: list[str] = []
    if p.is_dir():
        for f in sorted(p.rglob("*.json")):
            if _EXPORT_PATTERN.search(f.name):
                chunks.append(f.read_text(errors="replace"))
        if not chunks:
            raise FileNotFoundError(
                f"{p} contains no conversations.json or conversations-NNN.json")
    elif p.suffix.lower() == ".zip":
        with ZipFile(p) as z:
            members = sorted(n for n in z.namelist() if _EXPORT_PATTERN.search(n))
            if not members:
                raise FileNotFoundError(
                    f"{p} contains no conversations.json or conversations-NNN.json")
            for n in members:
                with z.open(n) as f:
                    chunks.append(f.read().decode("utf-8", errors="replace"))
    else:
        chunks.append(p.read_text(errors="replace"))
    n = 0
    for raw_text in chunks:
        data = json.loads(raw_text)
        if not isinstance(data, list):
            continue
        for conv in data:
            if not isinstance(conv, dict):
                continue
            conv_id = conv.get("uuid") or conv.get("id")
            if not conv_id:
                continue
            conv["_updated_at"] = conv.get("updated_at") or conv.get("created_at") or ""
            conv["_account"]    = account
            cache_save(_SERVICE, _cache_id(account, conv_id), conv)
            n += 1
    return n


# ─── Records / exchanges (from cache) ─────────────────────────────────────────

def _to_records(convs: list[dict]) -> list[dict]:
    records = []
    for conv in convs:
        account = conv.get("_account") or "default"
        for msg in _conv_messages(conv):
            if msg["sender"] != "assistant":
                continue
            out_tokens = max(len(msg["text"]) // 4, 0)
            if out_tokens == 0:
                continue
            tokens = {"input": 0, "output": out_tokens,
                      "cache_read": 0, "cache_write": 0}
            records.append({
                "tool":    TOOL_NAME,
                "model":   msg["model"] + " [est]",
                "project": _project_label(account),
                "ts":      msg["ts"],
                **tokens,
                "cost":    compute_cost(tokens, msg["model"]),
            })
    return records


def scan_claude_web() -> list[dict]:
    return _to_records(list(cache_iter(_SERVICE)))


def _extract_exchanges_claude_web() -> list[dict]:
    convs = list(cache_iter(_SERVICE))
    if not convs:
        return []
    exchanges: list[dict] = []
    for conv in convs:
        account = conv.get("_account") or "default"
        project = _project_label(account)
        current = None
        for m in _conv_messages(conv):
            if m["sender"] == "human":
                if current:
                    exchanges.append(current)
                current = {
                    "user_text":       m["text"][:500],
                    "assistant_texts": [],
                    "tool_errors":     [],
                    "tools_used":      defaultdict(int),
                    "num_turns":       0,
                    "model":           m["model"] + " [est]",
                    "project":         project,
                    "ts":              m["ts"],
                    "tokens":          {"input": max(len(m["text"]) // 4, 0),
                                        "output": 0,
                                        "cache_read": 0, "cache_write": 0},
                    "cost":            0.0,
                }
            elif m["sender"] == "assistant" and current is not None:
                current["num_turns"] += 1
                out_tokens = max(len(m["text"]) // 4, 0)
                current["tokens"]["output"] += out_tokens
                if m["text"]:
                    current["assistant_texts"].append(m["text"][:500])
                current["model"] = m["model"] + " [est]"
                current["cost"] += compute_cost(
                    {"input": 0, "output": out_tokens,
                     "cache_read": 0, "cache_write": 0},
                    m["model"],
                )
        if current:
            exchanges.append(current)
    return exchanges


def _collect_all_exchanges(cutoff: datetime, tool_filter: str | None = None,
                           cutoff_end: datetime | None = None) -> tuple[list[dict], dict[str, int]]:
    all_exchanges: list[dict] = []
    tool_counts: dict[str, int] = {}
    if tool_filter and tool_filter != TOOL_NAME:
        return all_exchanges, tool_counts
    exchanges = _extract_exchanges_claude_web()
    filtered = [ex for ex in exchanges
                if ex["ts"] and ex["ts"] >= cutoff
                and (cutoff_end is None or ex["ts"] < cutoff_end)]
    for ex in filtered:
        ex["tool"] = TOOL_NAME
    if filtered:
        all_exchanges.extend(filtered)
        tool_counts[TOOL_NAME] = len(filtered)
    _warm_worktree_cache(set(e.get("project") or "unknown" for e in all_exchanges))
    return all_exchanges, tool_counts


# ─── Main ─────────────────────────────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — {TOOL_NAME} (web export){RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")

    accounts = imported_accounts(_SERVICE)
    if not accounts:
        print(f"\n  {YELLOW}No imported claude.ai data found.{RESET}")
        print(f"  On claude.ai → Settings → Privacy → Export Data, wait")
        print(f"  for the email with the ZIP, then run:")
        print(f"    {BOLD}claude-web-token-usage --import <export.zip>{RESET}\n")
        return

    print(f"{DIM}  Reading cache for accounts: {', '.join(accounts)}{RESET}\n")
    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    records = scan_claude_web()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {MAGENTA}●{RESET} {TOOL_NAME:<10} {len(records):>6} assistant messages")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No usage data found in the given period.{RESET}\n")
        return

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, [], cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges)
    print(f"  {DIM}⚠ Token counts are estimated from text length "
          f"(claude.ai export does not include usage).{RESET}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"claude.ai": TOOL_NAME, "claude-web": TOOL_NAME, "claudeai": TOOL_NAME}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--activity", "--total", "--impact", "--region", "--export", "--period", "--since", "--tool",
    "--import", "--account", "--list-accounts", "--clean-cache",
    "--clear-imports",
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
    return _TOOL_ALIASES.get(raw)


def _parse_account(args: list[str]) -> str:
    if "--account" not in args:
        return "default"
    idx = args.index("--account")
    if idx + 1 >= len(args):
        return "default"
    return args[idx + 1].strip() or "default"


def show_help():
    print(f"""
{BOLD}claude-web-token-usage{RESET} — Usage from a claude.ai data export (estimated).

{BOLD}SETUP{RESET}
  On claude.ai → Settings → Privacy → Export Data. You'll get an
  email with a ZIP. Run:

    claude-web-token-usage --import path/to/export.zip [--account <name>]

  --account labels the import so multiple accounts (perso + work)
  appear as separate rows under CONSUMPTION BY PROJECT.

{BOLD}MODES{RESET}
  claude-web-token-usage                          Aggregated overview
  claude-web-token-usage --prompts  [-p]          Per-exchange detail
  claude-web-token-usage --anomalies              Anomaly detection
  claude-web-token-usage --activity               Activity calendar (by day)
  claude-web-token-usage  --total                  Compact totals (tokens + cost)
  claude-web-token-usage  --impact                 Energy & CO₂ estimate
  claude-web-token-usage --plan                   Cost breakdown + tips
  claude-web-token-usage --export   [file]        Export exchanges to JSON
  claude-web-token-usage --import <zip|json|dir>  Load the official export
                              [--account <name>]
  claude-web-token-usage --list-accounts          Show imported accounts
  claude-web-token-usage --clean-cache            Drop legacy orphan files
                                                  (pre-multi-account era)
  claude-web-token-usage --clear-imports          Drop imported convs
                              [--account <name>]  ...for one account only

{BOLD}FILTERS{RESET}
  --period <p>    all, hour, "5 hours", today, "7 days", "30 days", "3 months", "6 months", year

{BOLD}DATA SOURCE{RESET}
  {MAGENTA}claude.ai export{RESET} — local cache under
  {DIM}~/.cache/tokstat/web/claude.ai/{RESET}
""")


def cli():
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print(f"tokstat {__version__}")
        return
    if "--help" in args or "-h" in args:
        show_help()
        return

    if "--import" in args:
        idx = args.index("--import")
        if idx + 1 >= len(args) or args[idx + 1].startswith("-"):
            print(f"  {RED}--import requires a path (ZIP, JSON, or folder).{RESET}")
            sys.exit(1)
        account = _parse_account(args)
        try:
            n = _import_from_path(args[idx + 1], account)
        except Exception as e:
            print(f"  {RED}Import failed: {e}{RESET}")
            sys.exit(1)
        print(f"  Imported {BOLD}{n}{RESET} conversation(s) into account "
              f"{BOLD}{account}{RESET} "
              f"({DIM}cache: ~/.cache/tokstat/web/{_SERVICE}/{RESET}).")
        print(f"  Run {BOLD}claude-web-token-usage --period all{RESET} to view.")
        return

    if "--list-accounts" in args:
        accs = imported_accounts(_SERVICE)
        if not accs:
            print(f"  {DIM}No imported claude.ai accounts.{RESET}")
        else:
            print(f"  {BOLD}claude.ai imported accounts:{RESET}")
            for name in accs:
                print(f"    {MAGENTA}●{RESET} {name}")
        orphans = cache_orphans(_SERVICE)
        if orphans:
            print(f"  {DIM}{len(orphans)} orphan cache file(s) from older "
                  f"versions — run --clean-cache to remove.{RESET}")
        return

    if "--clean-cache" in args:
        n = clean_orphans(_SERVICE)
        kept = len(list(cache_iter(_SERVICE)))
        print(f"  Removed {BOLD}{n}{RESET} legacy orphan file(s). "
              f"{BOLD}{kept}{RESET} imported conversation(s) kept "
              f"({DIM}use --clear-imports to drop those too{RESET}).")
        return

    if "--clear-imports" in args:
        account = _parse_account(args) if "--account" in args else None
        n = clear_imports(_SERVICE, account)
        scope = f"account {BOLD}{account}{RESET}" if account else "all accounts"
        print(f"  Removed {BOLD}{n}{RESET} imported conversation(s) ({scope}).")
        return

    unknown = [a for a in args if a.startswith("-") and a not in _KNOWN_FLAGS]
    if unknown:
        print(f"\n  {RED}Unknown option(s): {', '.join(unknown)}{RESET}")
        print(f"  Run {BOLD}claude-web-token-usage --help{RESET} for usage.\n")
        sys.exit(1)

    period = _parse_period(args)
    tool = _parse_tool(args)

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
