#!/usr/bin/env python3
"""
claude-web-token-usage — Usage from logged-in claude.ai (web UI).

Authenticates with the user's `sessionKey` cookie and walks the same
private endpoints the web app uses. Token counts are **estimated** from
text length (chars / 4) — claude.ai does not expose per-message usage to
the client.

  Cookie source: copy the value of `sessionKey` from claude.ai DevTools.
  Storage:       `~/.config/tokstat/web-auth.json` (mode 0600), or env
                 `TOKSTAT_CLAUDE_AI_SESSION`.

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
    BOLD, DIM, RESET, YELLOW, RED, MAGENTA,
    TOOL_COLORS, PRICING,
    load_pricing, compute_cost,
    resolve_period,
    normalize_project, _warm_worktree_cache,
    show_overview_tables, show_prompts, show_anomalies, show_plan,
    export_conversations, _parse_period, print_update_notice,
)
from tokstat._web import (
    get_session, set_session, clear_session,
    http_get_json, cache_load, cache_save, cache_is_fresh,
)

TOOL_NAME = "Claude.ai"
TOOL_COLORS[TOOL_NAME] = MAGENTA

_SERVICE = "claude.ai"
_BASE    = "https://claude.ai/api"
_PROJECT = "claude.ai (web)"   # placeholder project for grouping


def _auth_headers() -> dict:
    sess = get_session(_SERVICE)
    if not sess:
        raise RuntimeError(
            "No claude.ai session cookie configured. "
            "Run: claude-web-token-usage --set-cookie <sessionKey-value>"
        )
    return {"Cookie": f"sessionKey={sess}", "Referer": "https://claude.ai/"}


# ─── Fetchers ────────────────────────────────────────────────────────────────

def _list_organizations() -> list[dict]:
    return http_get_json(f"{_BASE}/organizations", headers=_auth_headers()) or []


def _list_conversations(org_uuid: str) -> list[dict]:
    return http_get_json(
        f"{_BASE}/organizations/{org_uuid}/chat_conversations",
        headers=_auth_headers(),
    ) or []


def _get_conversation(org_uuid: str, conv_uuid: str) -> dict:
    return http_get_json(
        f"{_BASE}/organizations/{org_uuid}/chat_conversations/{conv_uuid}"
        "?tree=True&rendering_mode=raw",
        headers=_auth_headers(),
    )


def _sync_all() -> list[dict]:
    """Walk every org/conversation, returning the cached or freshly-fetched
    conversation payloads with their messages."""
    orgs = _list_organizations()
    out: list[dict] = []
    for org in orgs:
        org_uuid = org.get("uuid")
        if not org_uuid:
            continue
        for entry in _list_conversations(org_uuid):
            conv_id = entry.get("uuid")
            if not conv_id:
                continue
            updated = entry.get("updated_at")
            cached = cache_load(_SERVICE, conv_id)
            if cache_is_fresh(cached, updated):
                out.append(cached)
                continue
            try:
                detail = _get_conversation(org_uuid, conv_id)
            except Exception:
                if cached:
                    out.append(cached)
                continue
            detail["_updated_at"] = updated
            cache_save(_SERVICE, conv_id, detail)
            out.append(detail)
    return out


# ─── Parsing ────────────────────────────────────────────────────────────────

def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None


def _message_text(msg: dict) -> str:
    """Reassemble a message body from its `content` parts (or fallback)."""
    parts = msg.get("content") or []
    chunks: list[str] = []
    if isinstance(parts, list):
        for p in parts:
            if not isinstance(p, dict):
                continue
            if p.get("type") == "text":
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


def _to_records(convs: list[dict]) -> list[dict]:
    """One record per assistant message, tokens estimated from char count."""
    records = []
    for conv in convs:
        for msg in _conv_messages(conv):
            if msg["sender"] != "assistant":
                continue
            out_tokens = max(len(msg["text"]) // 4, 0)
            if out_tokens == 0:
                continue
            tokens = {"input": 0, "output": out_tokens,
                      "cache_read": 0, "cache_write": 0}
            model = msg["model"] + " [est]"
            records.append({
                "tool":    TOOL_NAME,
                "model":   model,
                "project": _PROJECT,
                "ts":      msg["ts"],
                **tokens,
                "cost":    compute_cost({"input": 0, "output": out_tokens,
                                         "cache_read": 0, "cache_write": 0},
                                        msg["model"]),
            })
    return records


def scan_claude_web() -> list[dict]:
    if not get_session(_SERVICE):
        return []
    try:
        convs = _sync_all()
    except Exception as e:
        print(f"  {RED}claude.ai fetch failed: {e}{RESET}", file=sys.stderr)
        return []
    return _to_records(convs)


def _extract_exchanges_claude_web() -> list[dict]:
    if not get_session(_SERVICE):
        return []
    try:
        convs = _sync_all()
    except Exception:
        return []

    exchanges: list[dict] = []
    for conv in convs:
        msgs = _conv_messages(conv)
        current = None
        for m in msgs:
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
                    "project":         _PROJECT,
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


# ─── Main ────────────────────────────────────────────────────────────────────

def main(period_name: str | None = None, tool_filter: str | None = None):
    print(f"\n{BOLD} Token Usage — {TOOL_NAME} (web){RESET}")
    print(f"{DIM}  Loading pricing from LiteLLM...{RESET}")
    load_pricing()
    if PRICING:
        print(f"  {DIM}{len(PRICING)} models loaded{RESET}")

    if not get_session(_SERVICE):
        print(f"\n  {YELLOW}No claude.ai session cookie configured.{RESET}")
        print(f"  Open claude.ai in a logged-in browser, DevTools → "
              f"Application → Cookies → copy {BOLD}sessionKey{RESET}, then:\n")
        print(f"    {BOLD}claude-web-token-usage --set-cookie <value>{RESET}\n")
        return

    print(f"{DIM}  Syncing conversations from claude.ai...{RESET}\n")
    try:
        cutoff, cutoff_end, period_label = resolve_period(period_name)
    except ValueError as e:
        print(f"  {RED}{e}{RESET}\n")
        return

    records = scan_claude_web()
    records = [r for r in records
               if r["ts"] >= cutoff and (cutoff_end is None or r["ts"] < cutoff_end)]

    if records:
        print(f"  {MAGENTA}●{RESET} {TOOL_NAME:<10} {len(records):>6} assistant messages from claude.ai")
    print(f"\n  Period: {BOLD}{period_label}{RESET}")

    if not records:
        print(f"\n  {YELLOW}No usage data found in the given period.{RESET}\n")
        return

    exchanges, _ = _collect_all_exchanges(cutoff, tool_filter, cutoff_end)
    show_overview_tables(records, [], cutoff, cutoff_end, period_label,
                         tool_filter, all_exchanges=exchanges)
    print(f"  {DIM}⚠ Token counts are estimated from text length "
          f"(claude.ai does not expose usage).{RESET}\n")


# ─── CLI ─────────────────────────────────────────────────────────────────────

_TOOL_ALIASES = {"claude.ai": TOOL_NAME, "claude-web": TOOL_NAME, "claudeai": TOOL_NAME}

_KNOWN_FLAGS = {
    "--help", "-h", "--version", "-V", "--prompts", "-p", "--anomalies",
    "--plan", "--export", "--period", "--since", "--tool",
    "--set-cookie", "--clear-cookie",
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
    return _TOOL_ALIASES.get(raw) or None


def show_help():
    print(f"""
{BOLD}claude-web-token-usage{RESET} — Usage from the claude.ai web UI (estimated).

{BOLD}SETUP{RESET}
  Copy the {BOLD}sessionKey{RESET} cookie value from claude.ai in your browser, then:
    claude-web-token-usage --set-cookie <value>
  Or export {BOLD}TOKSTAT_CLAUDE_AI_SESSION{RESET}=<value>.

{BOLD}MODES{RESET}
  claude-web-token-usage                      Aggregated overview
  claude-web-token-usage --prompts  [-p]      Per-exchange detail
  claude-web-token-usage --anomalies          Anomaly detection
  claude-web-token-usage --plan               Cost breakdown + tips
  claude-web-token-usage --export   [file]    Export exchanges to JSON
  claude-web-token-usage --set-cookie <v>     Store sessionKey cookie
  claude-web-token-usage --clear-cookie       Forget sessionKey cookie

{BOLD}FILTERS{RESET}
  --period <p>    all, hour, "5 hours", today, "7 days", "30 days", year

{BOLD}DATA SOURCE{RESET}
  {MAGENTA}claude.ai{RESET}  private endpoints under https://claude.ai/api/
              ✓ Conversations · ✓ Models · ~ Tokens (estimated)
""")


def cli():
    args = sys.argv[1:]
    if "--version" in args or "-V" in args:
        print(f"tokstat {__version__}")
        return
    if "--help" in args or "-h" in args:
        show_help()
        return

    if "--set-cookie" in args:
        idx = args.index("--set-cookie")
        if idx + 1 >= len(args):
            print(f"  {RED}--set-cookie requires a value.{RESET}")
            sys.exit(1)
        set_session(_SERVICE, args[idx + 1].strip())
        print(f"  {BOLD}claude.ai{RESET} session cookie stored "
              f"({DIM}~/.config/tokstat/web-auth.json{RESET}).")
        return
    if "--clear-cookie" in args:
        clear_session(_SERVICE)
        print(f"  claude.ai session cookie cleared.")
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
